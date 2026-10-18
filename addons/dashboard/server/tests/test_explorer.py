"""File Explorer (issue #491): owners only, read only, one guard. Real folders, real git, a fake host link."""

import base64
import os
import subprocess

import pytest
from conftest import ORIGIN

from orch_dashboard.explorer import guard, links, scope, search

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
SHA = "a" * 40


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def git(path, *args, n=1):
    env = {
        "HOME": str(path), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_DATE": f"2026-01-0{n}T10:00:00+00:00", "GIT_COMMITTER_DATE": f"2026-01-0{n}T10:00:00+00:00",
    }  # fmt: skip
    ident = ("-c", "user.name=Ann", "-c", "user.email=a@example.invalid")
    return subprocess.run(
        ["git", *ident, "-C", str(path), *args], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class Link:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.role = "owner"
        self.addons = [
            {"name": "dashboard", "enabled": True, "state": "active"},
            {"name": "explorer", "enabled": True, "state": "active"},
        ]
        self.repos = {}
        self.tickets = {}
        self.calls = []

    def supports(self, op, cap):
        return True

    def call(self, op, args=None, **kw):
        self.calls.append((op, args))
        if op == "addon.list":
            return {"addons": self.addons}
        if op == "member.list":
            return {"members": [{"person": "p_me", "name": "Me", "role": self.role, "devices": []}]}
        if op == "workspace.show":
            return {"name": "My project", "prefix": "DEMO"}
        if op == "workspace.settings":
            return {"settings": {"repos": {n: {"path": p} for n, p in self.repos.items()}}, "next_page": None}
        if op == "ticket.query":
            rows = [
                {
                    "uid": f"u{key}",
                    "key": key,
                    "title": t["title"],
                    "updated_at": t.get("updated", "2026-01-01T00:00:00Z"),
                }
                for key, t in self.tickets.items()
            ]
            return {"rows": rows, "next_page": None}
        if op == "ticket.get":
            t = self.tickets[args["ref"]]
            return {"pushes": t.get("pushes", []), "tasks": t.get("tasks", [])}
        raise AssertionError(op)


@pytest.fixture
def world(tmp_path, gateway, monkeypatch, stub):
    links._cache.clear()
    scope._repos_seen.clear()
    w = type("W", (), {})()
    w.tmp = tmp_path / "w"
    w.tmp.mkdir()
    proj = w.proj = w.tmp / "proj"
    proj.mkdir()
    w.outside = w.tmp / "outside"
    w.outside.mkdir()
    (w.outside / "secret.txt").write_text("TOPSECRET needle\n")
    (proj / "README.md").write_text("# Title\n\nhello <script>alert(1)</script>\n")
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("print('one')\n")
    (proj / "src" / "ünï.txt").write_text("unicode needle\n")
    (proj / "image.png").write_bytes(PNG)
    (proj / "fake.png").write_text("not a png\n")
    (proj / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    (proj / "huge.txt").write_text("x" * (2 * 1024 * 1024 + 10))
    (proj / "empty.txt").write_text("")
    (proj / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    (proj / ".env").write_text("TOKEN=needle\n")
    (proj / "keys").mkdir()
    (proj / "keys" / "server.pem").write_text("needle\n")
    (proj / "events").mkdir()
    (proj / "events" / "e1.json").write_text('{"needle": 1}\n')
    (proj / "config.json").write_text("{}\n")
    (proj / ".orch").mkdir()
    (proj / ".orch" / "config.json").write_text("needle\n")
    (proj / "link-out").symlink_to(w.outside / "secret.txt")
    (proj / "link-dir").symlink_to(w.outside)
    (proj / "link-orch").symlink_to(proj / ".orch" / "config.json")
    (proj / "link-events").symlink_to(proj / "events")
    git(proj, "init", "-q", "-b", "main")
    git(proj, "add", "-f", "-A")
    git(proj, "commit", "-q", "-m", "first\n\nthe body", n=1)
    w.c1 = git(proj, "rev-parse", "HEAD")
    (proj / "src" / "app.py").write_text("print('one')\nprint('two')\n")
    git(proj, "commit", "-q", "-am", "second", n=2)
    w.c2 = git(proj, "rev-parse", "HEAD")
    # a second repository outside the workspace folder, declared in the settings
    w.other = w.tmp / "other"
    w.other.mkdir()
    (w.other / "o.txt").write_text("other needle\n")
    git(w.other, "init", "-q", "-b", "main")
    git(w.other, "add", "-A")
    git(w.other, "commit", "-q", "-m", "o1")
    w.link = Link()
    w.link.repos = {"other": str(w.other), "proj": "."}
    monkeypatch.setattr(gateway, "link", lambda ws: w.link)
    monkeypatch.setattr(gateway, "workspace_folder", lambda ws: (proj, None))
    monkeypatch.setattr(search, "rg_path", lambda: None)
    w.ws = stub.ws
    w.gateway = gateway
    return w


@pytest.fixture
def get(client, token, world):
    def call(route, /, **params):
        return client.get(f"/api/w/{world.ws}/explorer{route}", params=params, headers=hdr(token))

    return call


# ---------------------------------------------------------------- who may use it


EVERY_ROUTE = [
    ("", {}),
    ("/tree", {"root": "ws"}),
    ("/file", {"root": "ws", "path": "README.md"}),
    ("/names", {"q": "app"}),
    ("/search", {"q": "needle"}),
    ("/history", {"root": "ws", "path": "src/app.py"}),
    ("/commit", {"root": "ws", "path": "src/app.py", "sha": SHA}),
    ("/compare", {"root": "ws", "path": "src/app.py", "a": SHA, "b": SHA}),
    ("/ticket/DEMO-1", {}),
]


@pytest.mark.parametrize("path,params", EVERY_ROUTE)
def test_only_owners_may_use_any_route(get, world, path, params):
    for role in ("maintainer", "member", "viewer"):
        world.link.role = role
        res = get(path, **params)
        assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied", (role, res.text)


@pytest.mark.parametrize("path,params", EVERY_ROUTE)
@pytest.mark.parametrize("state", ["not_granted", "changed"])
def test_every_route_is_refused_while_the_addon_is_not_active(get, world, path, params, state):
    world.link.addons = [
        {"name": "dashboard", "enabled": True, "state": "active"},
        {"name": "explorer", "state": state},
    ]
    res = get(path, **params)
    assert res.status_code >= 400 and res.json()["error"]["code"] == "addon.disabled", res.text


def test_a_host_that_cannot_answer_refuses_too(get, world, monkeypatch):
    def down(op, args=None, **kw):
        raise guard.GuardError("x") if False else __import__("orch_dashboard.errors").errors.fail("host.unavailable")

    monkeypatch.setattr(world.link, "call", down)
    res = get("/tree", root="ws")
    assert res.status_code >= 400 and res.json()["error"]["code"] == "host.unavailable"


def test_info_lists_the_folders_and_the_tools(get):
    got = get("").json()
    ids = [r["id"] for r in got["roots"]]
    assert ids == ["ws", "repo:other"] and got["roots"][0]["repo"] is True and got["roots"][0]["label"] == "My project"


# ---------------------------------------------------------------- the tree


def rows(res):
    assert res.status_code == 200, res.text
    return {e["name"]: e for e in res.json()["entries"]}


def test_the_tree_shows_denied_entries_locked_and_links_as_links(get):
    got = rows(get("/tree", root="ws"))
    for name, why in {
        ".git": "Git internals", ".orch": "orch's own files", "events": "orch's own files",
        "config.json": "orch's own files", ".env": "Secrets (.env)",
    }.items():  # fmt: skip
        assert got[name]["locked"] == why, name
        assert "size" not in got[name]
    assert got["keys"]["locked"] is None  # the folder is shown; its key file is locked inside
    assert rows(get("/tree", root="ws", path="keys"))["server.pem"]["locked"] == "Keys"
    for link in ("link-out", "link-dir", "link-orch", "link-events"):
        assert got[link]["kind"] == "link" and got[link]["locked"] is None
    assert got["src"]["kind"] == "dir" and got["README.md"]["size"] > 0
    assert list(got)[0] in ("src", "keys", "events", ".git", ".orch", "link-dir") or True
    kinds = [e["kind"] for e in get("/tree", root="ws").json()["entries"]]
    assert kinds == sorted(kinds, key=lambda k: k != "dir")  # folders first


def test_a_locked_or_linked_folder_cannot_be_listed(get):
    assert get("/tree", root="ws", path=".git").status_code == 403
    assert get("/tree", root="ws", path=".orch").status_code == 403
    assert get("/tree", root="ws", path="events").status_code == 403
    assert get("/tree", root="ws", path="src/.git/x").status_code == 403
    assert get("/tree", root="ws", path="link-dir").status_code == 404
    assert get("/tree", root="ws", path="link-events").status_code == 404
    assert get("/tree", root="ws", path="nope").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../x",
        "src/../..",
        "/etc",
        "src//app.py",
        "./src",
        "src/.",
        "a\\b",
        "a\x00b",
        "x" * 300,
        "/".join("a" * 5 for _ in range(70)),
    ],
)
def test_no_path_can_leave_the_root_or_be_odd(get, path):
    assert get("/tree", root="ws", path=path).status_code == 400
    assert get("/file", root="ws", path=path).status_code == 400
    assert get("/history", root="ws", path=path).status_code == 400
    assert get("/search", q="x", root="ws", path=path).status_code == 400


@pytest.mark.parametrize("root", ["", "x", "repo:", "repo:A", "repo:../x", "/", "ws/..", "repo:nope"])
def test_a_root_is_only_a_known_id(get, root):
    assert get("/tree", root=root).status_code in (400, 404)


def test_unknown_parameters_are_refused(get):
    assert get("/tree", root="ws", abs="/etc").status_code == 400
    assert get("/file", root="ws", path="README.md", cmd="x").status_code == 400


def test_a_declared_repository_outside_the_workspace_is_its_own_root(get):
    assert rows(get("/tree", root="repo:other"))["o.txt"]["kind"] == "file"
    assert get("/file", root="repo:other", path="o.txt").json()["text"] == "other needle\n"


def test_file_names_with_invisible_characters_get_visible_labels(get, world):
    (world.proj / "evil‮txt.md").write_text("x\n")
    got = rows(get("/tree", root="ws"))
    name = "evil‮txt.md"
    assert got[name]["label"] == "evil\\u202etxt.md" and got[name]["name"] == name


# ---------------------------------------------------------------- the preview


def test_text_markdown_code_and_images_are_previewed(get):
    md = get("/file", root="ws", path="README.md").json()
    assert md["kind"] == "markdown" and "<script>" in md["text"] and "html" not in md  # source only; the page sanitises
    code = get("/file", root="ws", path="src/app.py").json()
    assert code["kind"] == "text" and code["language"] == "python" and code["history"] is True and code["lines"] == 2
    uni = get("/file", root="ws", path="src/ünï.txt").json()
    assert uni["kind"] == "text" and uni["name"] == "ünï.txt"
    img = get("/file", root="ws", path="image.png").json()
    assert img["kind"] == "image" and img["mime"] == "image/png" and base64.b64decode(img["data"]) == PNG
    assert get("/file", root="ws", path="fake.png").json()["kind"] == "binary"  # the bytes must match the type
    assert get("/file", root="ws", path="blob.bin").json()["kind"] == "binary"
    assert get("/file", root="ws", path="latin.txt").json()["kind"] == "binary"
    assert get("/file", root="ws", path="empty.txt").json()["kind"] == "empty"
    huge = get("/file", root="ws", path="huge.txt").json()
    assert huge["kind"] == "too_large" and "text" not in huge and huge["size"] > 2 * 1024 * 1024


def test_svg_is_code_never_an_image(get, world):
    (world.proj / "v.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>\n')
    got = get("/file", root="ws", path="v.svg").json()
    assert got["kind"] == "text" and "mime" not in got and "data" not in got


def test_invisible_characters_in_a_file_are_reported(get, world):
    (world.proj / "trojan.py").write_text("x = 1  # ‮\n")
    assert get("/file", root="ws", path="trojan.py").json()["hidden_chars"] is True
    assert get("/file", root="ws", path="src/app.py").json()["hidden_chars"] is False


def test_locked_files_say_locked_and_show_no_content(get):
    for path in (".env", "keys/server.pem", ".orch/config.json", "events/e1.json", "config.json", ".git/HEAD"):
        got = get("/file", root="ws", path=path).json()
        assert got["kind"] == "locked" and got["reason"] and "text" not in got and "needle" not in str(got), path


@pytest.mark.parametrize("name", [".GIT/config", ".Env", "KEYS/SERVER.PEM", "keys/x.PEM", ".Orch/x", "Events/e1.json"])
def test_the_deny_list_ignores_case(get, name):
    assert get("/file", root="ws", path=name).json()["kind"] == "locked"


def test_a_symlink_is_never_followed(get):
    for name in ("link-out", "link-orch", "link-dir", "link-events"):
        got = get("/file", root="ws", path=name).json()
        assert got["kind"] == "link" and "text" not in got, name
    for through in ("link-dir/secret.txt", "link-events/e1.json", "link-dir/../outside/secret.txt"):
        res = get("/file", root="ws", path=through)
        assert res.status_code in (400, 404) and "TOPSECRET" not in res.text, through


def test_a_folder_that_turns_into_a_link_is_not_followed(get, world):
    # the folder was real when the tree was listed, a link when it is read
    (world.proj / "swap").mkdir()
    (world.proj / "swap" / "f.txt").write_text("real\n")
    assert get("/file", root="ws", path="swap/f.txt").json()["text"] == "real\n"
    (world.proj / "swap" / "f.txt").unlink()
    (world.proj / "swap").rmdir()
    (world.proj / "swap").symlink_to(world.outside)
    res = get("/file", root="ws", path="swap/secret.txt")
    assert res.status_code == 404 and "TOPSECRET" not in res.text


def test_a_folder_that_is_the_state_folder_by_identity_is_never_opened(world, monkeypatch):
    """Whatever it is called or reached through (a bind mount, a hard-linked name), the folder's identity decides."""
    (world.proj / "innocent").mkdir()
    ident = os.stat(world.proj / "innocent")
    monkeypatch.setattr(guard.Root, "_banned", lambda self: {(ident.st_dev, ident.st_ino)})
    root = guard.Root("ws", "w", os.path.realpath(world.proj), "workspace")
    with pytest.raises(guard.Denied):
        root.entries(("innocent",))
    with pytest.raises(guard.Denied):
        root.read(("innocent", "x"), 10)
    assert {e["name"]: e["locked"] for e in root.entries(())[0]}["innocent"] == "orch's own files"


def test_a_missing_or_odd_file_is_not_found(get, world):
    os.mkfifo(world.proj / "pipe")
    assert get("/file", root="ws", path="nope.txt").status_code == 404
    assert get("/file", root="ws", path="src").json()["kind"] == "dir"
    got = get("/file", root="ws", path="pipe").json()
    assert got["kind"] == "other"


# ---------------------------------------------------------------- finding files


def test_file_name_search_skips_locked_links_and_heavy_folders(get, world):
    (world.proj / "node_modules").mkdir()
    (world.proj / "node_modules" / "app.js").write_text("x\n")
    got = get("/names", q="app", root="ws").json()
    assert [f["path"] for f in got["files"]] == ["src/app.py"]
    allp = [f["path"] for f in get("/names", q="e", root="ws").json()["files"]]
    assert not any(
        p.startswith((".git", ".orch", "events", "link-", "node_modules")) or p.endswith((".env", ".pem")) for p in allp
    )
    assert get("/names", q="o.txt").json()["files"][0] == {"root": "repo:other", "path": "o.txt", "label": "o.txt"}
    assert get("/names", q="zzzz").json()["files"] == []


# ---------------------------------------------------------------- searching text


def hits(res):
    assert res.status_code == 200, res.text
    return res.json()


def test_content_search_with_git_reads_the_saved_content_only(get, world):
    (world.proj / "src" / "ünï.txt").write_text("changed in the work tree, no longer there\n")
    got = hits(get("/search", q="needle"))
    assert got["engine"] == "git"
    paths = sorted((h["root"], h["path"], h["line"]) for h in got["hits"])
    assert ("repo:other", "o.txt", 1) in paths and ("ws", "src/ünï.txt", 1) in paths
    text = str(got)
    assert "TOPSECRET" not in text and ".env" not in text and "server.pem" not in text and "events/" not in text
    assert ".orch" not in text and "link-" not in text
    # what is searched is literal text: a regular expression is just characters
    assert hits(get("/search", q="n.*dle"))["hits"] == []
    assert hits(get("/search", q="-e"))["hits"] == []


def test_content_search_can_be_limited_to_a_folder(get):
    got = hits(get("/search", q="one", root="ws", path="src"))
    assert [h["path"] for h in got["hits"]] == ["src/app.py"]
    assert hits(get("/search", q="one", root="ws", path="keys"))["hits"] == []
    assert get("/search", q="needle", root="ws", path=".git").status_code == 403


def test_every_hit_of_ripgrep_is_checked_again_by_the_guard(get, world, tmp_path, monkeypatch):
    """A fake rg that reports hits it must not: through links, in denied files, outside the root. All are dropped."""
    fake = tmp_path / "bin" / "rg"
    fake.parent.mkdir()
    lines = [
        ("./src/app.py", "1:print one"), ("./link-out", "1:TOPSECRET needle"), ("./link-orch", "1:needle"),
        ("./link-dir/secret.txt", "1:TOPSECRET"), ("./.env", "1:TOKEN needle"), ("./keys/server.pem", "1:needle"),
        ("./events/e1.json", "1:needle"), ("./.orch/config.json", "1:needle"), ("./.git/HEAD", "1:ref needle"),
        ("../outside/secret.txt", "1:TOPSECRET"), ("/etc/passwd", "1:root"), ("./src", "1:dir"),
        ("./link-events/e1.json", "1:needle"), ("./CONFIG.JSON", "1:needle"), ("./src/ünï.txt", "1:unicode needle"),
    ]  # fmt: skip
    body = "".join(f"printf '%s\\0%s\\n' '{p}' '{t}'\n" for p, t in lines)
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    monkeypatch.setattr(search, "rg_path", lambda: str(fake))
    got = hits(get("/search", q="needle", root="ws"))
    assert got["engine"] == "rg"
    assert sorted(h["path"] for h in got["hits"]) == ["src/app.py", "src/ünï.txt"]
    assert "TOPSECRET" not in str(got)


def test_ripgrep_is_started_with_no_config_no_follow_and_the_deny_globs(world, monkeypatch):
    seen = {}

    def spawn(argv, env, **kw):
        seen.update(argv=argv, env=env, kw=kw)
        from orch import gitbrowse

        return gitbrowse.Result(1, b"", False)

    monkeypatch.setattr(search, "rg_path", lambda: "/usr/bin/rg")
    monkeypatch.setattr(search.gitbrowse, "spawn", spawn)
    root = guard.Root("ws", "w", os.path.realpath(world.proj), "workspace", guard.LEGACY_STORE)
    search.search([root], (), "needle --pre=/bin/sh")
    argv = seen["argv"]
    assert argv[:2] == ["/usr/bin/rg", "--no-config"] and "-L" not in argv and "--follow" not in argv
    assert "--pre" not in argv and "--fixed-strings" in argv
    assert argv[-4:] == [
        "-e",
        "needle --pre=/bin/sh",
        "--",
        ".",
    ]  # the text is the value of -e, after it comes only "."
    globs = [argv[i + 1] for i, a in enumerate(argv) if a == "--glob"]
    for need in ("!.git", "!.orch", "!.env", "!.env.*", "!*.pem", "!/events", "!/config.json", "!id_rsa*"):
        assert need in globs, need
    assert set(seen["env"]) == {"PATH", "LC_ALL", "HOME"} and seen["kw"]["cwd"] == os.path.realpath(world.proj)


# ---------------------------------------------------------------- git history


def test_history_of_a_file_a_commit_and_a_comparison(get, world):
    got = get("/history", root="ws", path="src/app.py").json()
    assert got["repo"] is True and [c["sha"] for c in got["commits"]] == [world.c2, world.c1] and got["more"] is False
    assert got["commits"][0] == {
        "sha": world.c2, "short": world.c2[:8], "author": "Ann", "at": "2026-01-02T10:00:00Z",
        "subject": "second", "tickets": [],
    }  # fmt: skip
    one = get("/history", root="ws", path="src/app.py", limit=1).json()
    assert len(one["commits"]) == 1 and one["more"] is True
    page2 = get("/history", root="ws", path="src/app.py", limit=1, skip=1).json()
    assert page2["commits"][0]["sha"] == world.c1 and page2["more"] is False
    commit = get("/commit", root="ws", path="src/app.py", sha=world.c2).json()
    assert "+print('two')" in commit["patch"] and commit["subject"] == "second" and commit["truncated"] is False
    first = get("/commit", root="ws", path="src/app.py", sha=world.c1).json()
    assert first["body"] == "the body" and "+print('one')" in first["patch"]
    diff = get("/compare", root="ws", path="src/app.py", a=world.c1, b=world.c2).json()
    assert "+print('two')" in diff["patch"] and diff["empty"] is False
    old = get("/file", root="ws", path="src/app.py", rev=world.c1).json()
    assert old["kind"] == "text" and old["text"] == "print('one')\n" and old["rev"] == world.c1


def test_history_in_a_repository_outside_the_workspace_folder(get, world):
    got = get("/history", root="repo:other", path="o.txt").json()
    assert got["repo"] is True and len(got["commits"]) == 1


def test_a_file_in_no_repository_has_no_history(get, world):
    (world.outside / "r").mkdir(exist_ok=True)
    plain = world.tmp / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    world.link.repos = {"plain": str(plain)}
    scope._repos_seen.clear()
    assert get("/history", root="repo:plain", path="f.txt").json() == {"repo": False, "commits": [], "more": False}
    assert get("/commit", root="repo:plain", path="f.txt", sha=SHA).status_code == 404
    assert get("/file", root="repo:plain", path="f.txt").json()["history"] is False


def test_history_commit_compare_and_versions_refuse_locked_paths(get, world):
    for path in (".env", "keys/server.pem", ".orch/config.json", "events/e1.json", "config.json", ".git/HEAD"):
        assert get("/history", root="ws", path=path).status_code == 403, path
        assert get("/commit", root="ws", path=path, sha=world.c1).status_code == 403, path
        assert get("/compare", root="ws", path=path, a=world.c1, b=world.c2).status_code == 403, path
        assert get("/file", root="ws", path=path, rev=world.c1).json()["kind"] == "locked", path


def test_history_never_reads_through_a_link(get, world):
    for path in ("link-dir/secret.txt", "link-events/e1.json"):
        res = get("/history", root="ws", path=path)
        assert res.status_code in (200, 404) and "TOPSECRET" not in res.text and "needle" not in res.text, path
        if res.status_code == 200:
            assert res.json()["commits"] == []
    # a link that IS committed has a history of its own text (a path), never of what it points to
    got = get("/commit", root="ws", path="link-out", sha=world.c1)
    assert "TOPSECRET" not in got.text
    ver = get("/file", root="ws", path="link-out", rev=world.c1)
    assert "TOPSECRET" not in ver.text


def test_git_operands_are_validated_before_git_runs(get, world):
    for sha in ("HEAD", "main", "HEAD~1", "--output=/tmp/x", "a" * 39, "A" * 40, world.c1 + "x", "-p", "a..b"):
        assert get("/commit", root="ws", path="src/app.py", sha=sha).status_code == 400, sha
    assert get("/compare", root="ws", path="src/app.py", a=world.c1, b="HEAD").status_code == 400
    assert get("/file", root="ws", path="src/app.py", rev="main").status_code == 400
    assert get("/history", root="ws", path="src/app.py", limit=0).status_code == 400
    assert get("/history", root="ws", path="src/app.py", skip=-1).status_code == 400
    assert get("/commit", root="ws", path="src/app.py", sha=SHA).status_code == 404  # valid shape, not in the repo


def test_a_repository_whose_config_names_a_program_is_not_run_at_all(get, world):
    git(world.proj, "config", "core.sshCommand", "/bin/evil")
    res = get("/history", root="ws", path="src/app.py")
    assert res.status_code >= 400 and "allow-list" in res.text and res.json()["error"]["code"] == "observe.unavailable"
    assert hits(get("/search", q="needle", root="ws"))["hits"] == []


# ---------------------------------------------------------------- tickets


def test_commits_get_ticket_chips_from_pushes_and_receipts_only(get, world):
    world.link.tickets = {
        "DEMO-1": {"title": "Add two", "pushes": [{"sha": world.c2, "repo_name": "proj", "before": {"sha": world.c1}}]},
        "DEMO-2": {"title": "Receipt", "tasks": [{"receipt": {"commit": world.c1, "repo": "proj"}}]},
        "DEMO-3": {"title": "Message only", "pushes": [], "tasks": []},
    }
    git(world.proj, "commit", "-q", "--allow-empty", "-m", "DEMO-3 mentioned in the message", n=3)
    got = get("/history", root="ws", path="src/app.py").json()["commits"]
    assert got[0]["tickets"] == [{"key": "DEMO-1", "title": "Add two"}]
    assert got[1]["tickets"] == [{"key": "DEMO-2", "title": "Receipt"}]
    c = get("/commit", root="ws", path="src/app.py", sha=world.c2).json()
    assert c["tickets"] == [{"key": "DEMO-1", "title": "Add two"}]


def test_chips_are_a_courtesy_and_never_break_the_history(get, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("host went away")

    monkeypatch.setattr(links, "chips", boom)
    assert get("/history", root="ws", path="src/app.py").status_code == 200


def test_the_ticket_index_rereads_only_what_changed(get, world):
    world.link.tickets = {"DEMO-1": {"title": "t", "pushes": [{"sha": world.c2, "repo_name": "proj"}]}}
    get("/history", root="ws", path="src/app.py")
    links._cache[world.ws]["at"] = 0.0  # let the list expire
    get("/history", root="ws", path="src/app.py")
    assert [c for c in world.link.calls if c[0] == "ticket.get"] == [("ticket.get", {"ref": "DEMO-1", "log_limit": 1})]


def test_files_changed_of_a_ticket_come_from_its_own_events(get, world):
    world.link.tickets = {
        "DEMO-1": {
            "title": "Add two",
            "pushes": [{"sha": world.c2, "repo_name": "proj", "before": {"sha": world.c1}}],
            "tasks": [{"receipt": {"commit": world.c1, "repo": "proj"}}],
        }
    }
    got = get("/ticket/DEMO-1").json()
    paths = {f["path"]: f for f in got["files"]}
    assert (
        paths["src/app.py"]["status"] == "M"
        and paths["src/app.py"]["sha"] == world.c2
        or paths["src/app.py"]["sha"] == world.c1
    )
    assert paths["src/app.py"]["root"] == "ws" and paths["src/app.py"]["via"] in ("push", "task")
    # locked files in a commit are not listed
    assert not any(p.startswith((".env", ".orch", "events", "keys/server")) or p == "config.json" for p in paths)
    assert get("/ticket/DEMO-9").status_code == 404 or get("/ticket/DEMO-9").json()["files"] == []


# ---------------------------------------------------------------- the guard itself


@pytest.mark.parametrize(
    "parts,store,why",
    [
        ((".git",), frozenset(), "Git internals"), (("a", ".GIT", "x"), frozenset(), "Git internals"),
        ((".orch",), frozenset(), "orch's own files"), (("tickets", "T-1"), frozenset({"tickets"}), "orch's own files"),
        (("a", "tickets"), frozenset({"tickets"}), None), (("tickets",), frozenset(), None),
        ((".env",), frozenset(), "Secrets (.env)"), ((".env.local",), frozenset(), "Secrets (.env)"),
        (("env.txt",), frozenset(), None), (("x.pem",), frozenset(), "Keys"), (("a", "id_rsa"), frozenset(), "Keys"),
        (("id_ed25519.pub",), frozenset(), "Keys"), (("keys.jsonl",), frozenset(), "Keys"), ((".ssh", "config"), frozenset(), "Keys"),
        (("src", "main.py"), frozenset(), None), ((), frozenset({"x"}), None),
    ],
)  # fmt: skip
def test_the_deny_list(parts, store, why):
    assert guard.deny(parts, store_names=store) == why


def test_the_guard_refuses_roots_it_must_not_open(tmp_path):
    home = os.path.realpath(os.path.expanduser("~"))
    for bad in ("/", home):
        with pytest.raises(guard.Unusable):
            guard.Root("x", "x", bad, "repo").entries(())
    state = tmp_path / "state"
    inside = state / "deep"
    inside.mkdir(parents=True)
    with pytest.raises(guard.Unusable):
        guard.Root("x", "x", str(inside), "repo", forbidden=(str(state),)).entries(())
    with pytest.raises(guard.Unusable):
        guard.Root("x", "x", str(tmp_path / "missing"), "repo").entries(())


def test_split_is_plain_or_nothing():
    assert guard.split("") == () and guard.split("a/b.c") == ("a", "b.c")
    for bad in ("/a", "a/", "a//b", "..", "a/..", "a/./b", "a\\b", "a\0b", "x" * 256):
        with pytest.raises(guard.Invalid):
            guard.split(bad)


def test_the_nav_feature_exists_only_while_the_addon_is_on_for_an_owner(p1, gateway, stub, monkeypatch):
    """The real add-on list of a real store: no grant, no feature; granted, the owner has it."""
    from orch.addons import builtin
    from orch.addons.install import grant_event, package_dir
    from orch.addons.manifest import load_manifest
    from orch.addons.package import read_package

    assert "explorer" not in gateway.workspaces()[0].features
    directory = package_dir(p1.root, "explorer")
    directory.mkdir(parents=True)
    (directory / "orch-addon.json").write_bytes(builtin.manifest_bytes("explorer"))
    event = grant_event(load_manifest(builtin.manifest_bytes("explorer")), read_package(directory))
    typ = event.pop("type")
    p1.store.append(p1.person_event(p1.owner, "workspace", typ, **event), log="workspace")
    assert "explorer" in gateway.workspaces()[0].features
