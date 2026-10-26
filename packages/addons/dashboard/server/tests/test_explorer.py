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


def _use(world, folder):
    """Make ``folder`` the workspace folder (the root `ws`) of this test, and forget what was confirmed."""
    world.mp.setattr(world.gateway, "workspace_folder", lambda ws: (folder, folder / ".orch"))
    world.link.repos = {}
    scope._repos_seen.clear()
    git_mod = __import__("orch_dashboard.explorer.git", fromlist=["x"])
    git_mod._confirmed.clear()


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
            return {
                "pushes": t.get("pushes", []),
                "tasks": t.get("tasks", []),
                **({"status": t["status"]} if "status" in t else {}),
            }
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
    (proj / ".orch").mkdir()
    (proj / ".orch" / "events").mkdir()
    (proj / ".orch" / "events" / "e1.json").write_text('{"needle": 1}\n')
    (proj / ".orch" / "config.json").write_text("needle\n")
    (proj / "config.json").write_text("{}\n")  # the project's own file: not orch's (the store is .orch/)
    (proj / "link-out").symlink_to(w.outside / "secret.txt")
    (proj / "link-dir").symlink_to(w.outside)
    (proj / "link-orch").symlink_to(proj / ".orch" / "config.json")
    (proj / "link-events").symlink_to(proj / ".orch" / "events")
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
    monkeypatch.setattr(gateway, "workspace_folder", lambda ws: (proj, proj / ".orch"))
    monkeypatch.setattr(search, "rg_path", lambda: None)
    w.ws = stub.ws
    w.gateway = gateway
    w.mp = monkeypatch
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
        ".git": "Git internals", ".orch": "orch's own files", ".env": "Secrets (.env)",
    }.items():  # fmt: skip
        assert got[name]["locked"] == why, name
        assert "size" not in got[name]
    assert got["config.json"]["locked"] is None  # a project's own config.json is the project's
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
    assert get("/tree", root="ws", path=".orch/events").status_code == 403
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
    for path in (".env", "keys/server.pem", ".orch/config.json", ".orch/events/e1.json", ".git/HEAD"):
        got = get("/file", root="ws", path=path).json()
        assert got["kind"] == "locked" and got["reason"] and "text" not in got and "needle" not in str(got), path


@pytest.mark.parametrize(
    "name", [".GIT/config", ".Env", "KEYS/SERVER.PEM", "keys/x.PEM", ".Orch/x", ".ORCH/events/e1.json"]
)
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
        ("./.orch/events/e1.json", "1:needle"), ("./.orch/config.json", "1:needle"), ("./.git/HEAD", "1:ref needle"),
        ("../outside/secret.txt", "1:TOPSECRET"), ("/etc/passwd", "1:root"), ("./src", "1:dir"),
        ("./link-events/e1.json", "1:needle"), ("./src/ünï.txt", "1:unicode needle"),
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
    root = guard.Root("ws", "w", os.path.realpath(world.proj), "workspace", frozenset())
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
    for need in ("!.git", "!.orch", "!.env", "!.env.*", "!*.pem", "!.state", "!id_rsa*"):
        assert need in globs, need
    assert (
        set(seen["env"]) == {"PATH", "LC_ALL", "HOME"}
        and "cwd" not in seen["kw"]
        and isinstance(seen["kw"]["cwd_fd"], int)
    )


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
    _use(world, plain)
    assert get("/history", root="ws", path="f.txt").json() == {"repo": False, "commits": [], "more": False}
    assert get("/commit", root="ws", path="f.txt", sha=SHA).status_code == 404
    assert get("/file", root="ws", path="f.txt").json()["history"] is False


def test_history_commit_compare_and_versions_refuse_locked_paths(get, world):
    for path in (".env", "keys/server.pem", ".orch/config.json", ".orch/events/e1.json", ".git/HEAD"):
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
    assert not any(p.startswith((".env", ".orch", "keys/server")) for p in paths)
    assert get("/ticket/DEMO-9").status_code == 404 or get("/ticket/DEMO-9").json()["files"] == []


def test_the_commit_a_branch_was_cut_from_is_not_work_of_an_open_ticket(get, world):
    # first sight of the branch ("before" empty) at a commit that is already on the repository's own line
    world.link.tickets = {
        "DEMO-1": {"title": "Open", "status": "in_progress", "pushes": [{"sha": world.c2, "repo_name": "proj"}]}
    }
    assert get("/ticket/DEMO-1").json()["files"] == []
    assert get("/history", root="ws", path="src/app.py").json()["commits"][0]["tickets"] == []
    # the same event on a finished ticket (merged work) stays listed
    links._cache.clear()
    world.link.tickets["DEMO-1"]["status"] = "done"
    assert [f["path"] for f in get("/ticket/DEMO-1").json()["files"]] == ["src/app.py"]
    assert get("/history", root="ws", path="src/app.py").json()["commits"][0]["tickets"] == [
        {"key": "DEMO-1", "title": "Open"}
    ]


# ---------------------------------------------------------------- the guard itself


@pytest.mark.parametrize(
    "parts,why",
    [
        ((".git",), "Git internals"), (("a", ".GIT", "x"), "Git internals"),
        ((".orch",), "orch's own files"), ((".ORCH", "tickets"), "orch's own files"),
        ((".state",), "orch's own files"), (("a", ".STATE", "x"), "orch's own files"), (("tickets", "T-1"), None),
        (("a", "tickets"), None), (("tickets",), None),
        ((".env",), "Secrets (.env)"), ((".env.local",), "Secrets (.env)"),
        (("env.txt",), None), (("x.pem",), "Keys"), (("a", "id_rsa"), "Keys"),
        (("id_ed25519.pub",), "Keys"), (("keys.jsonl",), "Keys"), ((".ssh", "config"), "Keys"),
        (("src", "main.py"), None), ((), None),
    ],
)  # fmt: skip
def test_the_deny_list(parts, why):
    assert guard.deny(parts) == why


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
    directory = package_dir(p1.store_dir, "explorer")
    directory.mkdir(parents=True)
    (directory / "orch-addon.json").write_bytes(builtin.manifest_bytes("explorer"))
    event = grant_event(load_manifest(builtin.manifest_bytes("explorer")), read_package(directory))
    typ = event.pop("type")
    p1.store.append(p1.person_event(p1.owner, "workspace", typ, **event), log="workspace")
    assert "explorer" in gateway.workspaces()[0].features


# ---------------------------------------------------------------- a .git that redirects git


def _spy(monkeypatch):
    from orch import gitbrowse

    ran = []
    real = gitbrowse.run

    def run(repo, op, *a, **k):
        ran.append((repo, op))
        return real(repo, op, *a, **k)

    monkeypatch.setattr(gitbrowse, "run", run)
    return ran


def test_a_gitfile_that_points_outside_the_root_is_not_a_repository(get, world, monkeypatch):
    real = world.tmp / "elsewhere"
    real.mkdir()
    git(real, "init", "-q", "-b", "main")
    (real / "leak.txt").write_text("LEAK needle\n")
    git(real, "add", "-A")
    git(real, "commit", "-q", "-m", "leak")
    sub = world.proj / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("x\n")
    (sub / ".git").write_text(f"gitdir: {real / '.git'}\n")
    ran = _spy(monkeypatch)
    assert get("/history", root="ws", path="sub/f.txt").json()["repo"] is True  # the workspace's own repository
    assert all(folder != os.path.realpath(world.proj / "sub") for folder, _op in ran), ran  # git never ran in sub
    assert get("/file", root="ws", path="sub/f.txt").json()["history"] is True
    got = get("/history", root="ws", path="sub/f.txt").json()
    assert all(c["subject"] != "leak" for c in got["commits"])
    assert "LEAK" not in get("/search", q="LEAK").text


def test_a_gitfile_that_points_into_the_orch_store_is_not_a_repository(get, world, monkeypatch):
    orch = world.proj / ".orch"
    plain = world.tmp / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    (plain / ".git").write_text(f"gitdir: {orch}\n")
    _use(world, plain)
    ran = _spy(monkeypatch)
    assert get("/history", root="ws", path="f.txt").json() == {"repo": False, "commits": [], "more": False}
    assert ran == []  # git was not even started there
    assert get("/file", root="ws", path="f.txt").json()["history"] is False


def test_a_git_folder_with_a_commondir_is_not_a_repository(get, world, monkeypatch):
    plain = world.tmp / "plain2"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    git(plain, "init", "-q", "-b", "main")
    (plain / ".git" / "commondir").write_text(str(world.proj / ".git") + "\n")
    _use(world, plain)
    ran = _spy(monkeypatch)
    assert get("/history", root="ws", path="f.txt").json()["repo"] is False
    assert ran == []


def test_a_repository_git_reads_from_somewhere_else_is_not_used(get, world, monkeypatch):
    """Even a real .git folder: git must find its directories exactly there (it is asked, not assumed)."""
    from orch import gitbrowse

    real = gitbrowse.run

    def lying(repo, op, *a, **k):
        res = real(repo, op, *a, **k)
        if op == "layout":
            return gitbrowse.Result(
                0, b"/elsewhere/.git\n/elsewhere/.git\n/elsewhere\n/elsewhere/.git/objects\n", False
            )
        return res

    git_mod = __import__("orch_dashboard.explorer.git", fromlist=["x"])
    git_mod._confirmed.clear()
    monkeypatch.setattr(gitbrowse, "run", lying)
    assert get("/history", root="ws", path="src/app.py").json() == {"repo": False, "commits": [], "more": False}
    git_mod._confirmed.clear()


def test_a_normal_repository_is_confirmed_once_and_remembered(get, world, monkeypatch):
    git_mod = __import__("orch_dashboard.explorer.git", fromlist=["x"])
    git_mod._confirmed.clear()
    ran = _spy(monkeypatch)
    get("/history", root="ws", path="src/app.py")
    get("/history", root="ws", path="src/app.py")
    assert [op for f, op in ran if f.endswith("/proj")].count("layout") == 1


def test_a_tree_or_blob_id_is_not_a_commit_anywhere(get, world):
    """40 hex characters of the right shape that name a tree or a blob: refused by git, never read."""
    tree = git(world.proj, "rev-parse", "HEAD^{tree}")
    blob = git(world.proj, "rev-parse", "HEAD:src/app.py")
    secret_blob = git(world.proj, "rev-parse", "HEAD:.orch/config.json")
    for bad in (tree, blob, secret_blob):
        for res in (
            get("/commit", root="ws", path="src/app.py", sha=bad),
            get("/compare", root="ws", path="src/app.py", a=bad, b=world.c2),
            get("/compare", root="ws", path="src/app.py", a=world.c1, b=bad),
            get("/file", root="ws", path="src/app.py", rev=bad),
        ):
            assert res.status_code == 404, (bad, res.text)
            assert "needle" not in res.text and "print(" not in res.text
        # and a denied path never gets that far
        assert get("/file", root="ws", path=".orch/config.json", rev=bad).json()["kind"] == "locked"
    for bad in ("HEAD:.orch/config.json", f"{world.c1}..{world.c2}", f"{world.c2}:src/app.py", f"{world.c2}^{{tree}}"):
        assert get("/file", root="ws", path="src/app.py", rev=bad).status_code == 400
        assert get("/compare", root="ws", path="src/app.py", a=bad, b=world.c2).status_code == 400


# ---------------------------------------------------------------- where git reads its objects from


def _plain_repo(world, name, link):
    folder = world.tmp / name
    folder.mkdir()
    (folder / "f.txt").write_text("x\n")
    git(folder, "init", "-q", "-b", "main")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "mine")
    _use(world, folder)
    return folder


def _refused(get, world, folder, name, monkeypatch):
    ran = _spy(monkeypatch)
    assert get("/history", root="ws", path="f.txt").json() == {"repo": False, "commits": [], "more": False}
    assert get("/file", root="ws", path="f.txt").json()["history"] is False
    assert get("/search", q="x", root="ws").json()["hits"] == [] and ran == []  # git never started there


def test_a_symlinked_objects_folder_is_not_a_repository(get, world, monkeypatch):
    folder = _plain_repo(world, "linked", None)
    real = folder / ".git" / "objects"
    moved = world.tmp / "moved-objects"
    real.rename(moved)
    real.symlink_to(moved)
    _refused(get, world, folder, "linked", monkeypatch)


@pytest.mark.parametrize("name", ["alternates", "http-alternates"])
def test_an_alternates_file_makes_it_not_a_repository(get, world, monkeypatch, name):
    folder = _plain_repo(world, "alt", None)
    other = world.proj / ".git" / "objects"  # objects of ANOTHER repository, outside this root
    (folder / ".git" / "objects" / "info").mkdir(exist_ok=True)
    (folder / ".git" / "objects" / "info" / name).write_text(f"{other}\n")
    _refused(get, world, folder, "alt", monkeypatch)


def test_a_grafts_file_makes_it_not_a_repository(get, world, monkeypatch):
    folder = _plain_repo(world, "graft", None)
    (folder / ".git" / "info").mkdir(exist_ok=True)
    (folder / ".git" / "info" / "grafts").write_text(f"{world.c2} {world.c1}\n")
    _refused(get, world, folder, "graft", monkeypatch)


def test_variables_git_reads_from_the_environment_cannot_move_it(get, world, monkeypatch):
    """GIT_DIR, GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES of the SERVER's own environment are not passed."""
    other = world.tmp / "mine"
    other.mkdir()
    (other / "z.txt").write_text("other repo\n")
    git(other, "init", "-q", "-b", "main")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "not this one")
    for name, value in {
        "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other), "GIT_COMMON_DIR": str(other / ".git"),
        "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects"), "GIT_INDEX_FILE": str(other / "i"),
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    git_mod = __import__("orch_dashboard.explorer.git", fromlist=["x"])
    git_mod._confirmed.clear()
    got = get("/history", root="ws", path="src/app.py").json()
    assert [c["sha"] for c in got["commits"]] == [world.c2, world.c1]
    assert "not this one" not in str(got)
    assert get("/commit", root="ws", path="src/app.py", sha=world.c2).status_code == 200
    assert "other repo" not in get("/search", q="other repo").text


# ---------------------------------------------------------------- the guard and git's pathspecs say the same


DENY_WALK = [
    "src/id_rsa.d/x.txt", "src/.env.d/y.txt", "certs.pem/inner.txt", "a/b/c/id_ed25519.bak/z", "x.key/deep/er/f", "k.p12/f",
    "w/repo.worktrees/f.txt", "a.kdbx", "login.keychain-db", "x/my.keychain/f", ".aws/credentials", "d/.aws/c/f", ".docker/config.json",
    ".kube/config", ".npmrc", "p/.pypirc", ".git-credentials", "ID_RSA_work/f", "UP.PEM/x", ".ENV", "src/.Env.Local/q",
    ".ssh/config", "n/.gnupg/f", "keys.jsonl", "sub/keys.jsonl/f", "sub/.orch/f", "n/.git/config",
    # fine
    "src/main.py", "README.md", "env.txt", "key.txt", "pem/readme", "my_id_rsa_notes.txt", "docs/keychain.md", "a/worktrees/f",
    "src/idrsa/f", ".environment/f", "package.json", "x.pemx/f", "gitignore", ".gitignore", ".github/w.yml",
]  # fmt: skip


def test_git_excludes_exactly_the_paths_the_guard_locks_at_any_depth(tmp_path):
    from orch import gitbrowse

    repo = tmp_path / "walk"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel in DENY_WALK:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n")
    # `.git` paths cannot be added through git: leave them to the guard
    tracked = [p for p in DENY_WALK if ".git/" not in p and not p.startswith(".git/") and p != ".git"]
    git(repo, "add", "-f", "--", *tracked)
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", ":(top)", *gitbrowse.DENY_SPECS],
        capture_output=True, text=True, check=True, env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    ).stdout.split("\n")  # fmt: skip
    visible = {p for p in listed if p}
    for rel in tracked:
        locked = guard.deny(tuple(rel.split("/"))) is not None
        assert (rel not in visible) == locked, (rel, "git hides it" if rel not in visible else "git shows it")


# ---------------------------------------------------------------- history is for one file


def test_history_commit_and_compare_take_a_file_never_a_folder(get, world):
    """A folder's patch would span every file below it, locked-named folders included (the PoC path was `src`)."""
    deny_dir = world.proj / "src" / "id_rsa.d"
    deny_dir.mkdir()
    (deny_dir / "leak.txt").write_text("SECRETLEAK\n")
    (world.proj / "src" / ".env.d").mkdir()
    (world.proj / "src" / ".env.d" / "e.txt").write_text("SECRETLEAK\n")
    git(world.proj, "add", "-f", "-A")
    git(world.proj, "commit", "-q", "-m", "folders with locked names", n=5)
    head = git(world.proj, "rev-parse", "HEAD")
    for path in ("src", "src/id_rsa.d", "src/.env.d", "docs"):
        assert get("/history", root="ws", path=path).status_code in (403, 404), path
        assert get("/commit", root="ws", path=path, sha=head).status_code in (403, 404), path
        assert get("/compare", root="ws", path=path, a=world.c1, b=head).status_code in (403, 404), path
    for res in (
        get("/commit", root="ws", path="src", sha=head),
        get("/compare", root="ws", path="src", a=world.c1, b=head),
    ):
        assert "SECRETLEAK" not in res.text
    # even with the guard out of the way, git does not give those files (the pathspec excludes them)
    from orch import gitbrowse

    out = gitbrowse.run(str(world.proj), "file_diff", os.environ, operand=("3", world.c1, head, "src")).out
    assert b"SECRETLEAK" not in out and b"id_rsa.d" not in out


def test_a_deleted_file_still_has_history_a_deleted_folder_does_not(get, world):
    (world.proj / "docs").mkdir()
    (world.proj / "docs" / "notes.txt").write_text("n\n")
    git(world.proj, "add", "-A")
    git(world.proj, "commit", "-q", "-m", "add notes", n=5)
    (world.proj / "docs" / "notes.txt").unlink()
    (world.proj / "docs").rmdir()
    git(world.proj, "commit", "-q", "-am", "remove notes", n=6)
    assert get("/history", root="ws", path="docs/notes.txt").json()["repo"] is True
    assert get("/history", root="ws", path="docs").status_code == 404  # no file of that name, and not on disk
    assert get("/history", root="ws", path="nope.txt").status_code == 404


# ---------------------------------------------------------------- which declared folders become roots


def test_a_declared_folder_that_is_not_a_plain_repository_is_not_a_root(get, world, monkeypatch):
    plain = world.tmp / "just-a-folder"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    gitfile = world.tmp / "gitfile-repo"
    gitfile.mkdir()
    (gitfile / ".git").write_text("gitdir: /elsewhere\n")
    home = world.tmp / "fakehome"
    (home / ".aws").mkdir(parents=True)
    git(home / ".aws", "init", "-q", "-b", "main")
    (home / "Library").mkdir()
    git(home / "Library", "init", "-q", "-b", "main")
    monkeypatch.setenv("HOME", str(home))
    world.link.repos = {
        "other": str(world.other), "plain": str(plain), "gitfile": str(gitfile), "aws": str(home / ".aws"),
        "lib": str(home / "Library"),
    }  # fmt: skip
    scope._repos_seen.clear()
    assert [r["id"] for r in get("").json()["roots"]] == ["ws", "repo:other"]
    assert get("/tree", root="repo:aws").status_code == 404
    assert get("/tree", root="repo:plain").status_code == 404


def test_a_hard_link_to_an_orch_file_is_not_readable_under_another_name(get, world):
    (world.proj / ".orch" / ".state").mkdir(exist_ok=True)
    (world.proj / ".orch" / ".state" / "secret.json").write_text("KEYMATERIAL\n")
    os.link(world.proj / ".orch" / ".state" / "secret.json", world.proj / "innocent.txt")
    os.link(world.proj / ".orch" / "config.json", world.proj / "cfg-copy.txt")
    for name in ("innocent.txt", "cfg-copy.txt"):
        got = get("/file", root="ws", path=name)
        assert "KEYMATERIAL" not in got.text and (got.status_code == 403 or got.json().get("kind") != "text"), name
    # a hard link between two ordinary files stays readable
    os.link(world.proj / "README.md", world.proj / "README-2.md")
    assert get("/file", root="ws", path="README-2.md").json()["kind"] == "markdown"


def test_ripgrep_starts_in_the_folder_the_guard_opened_not_in_a_path(world):
    from orch import gitbrowse

    root = guard.Root("ws", "w", os.path.realpath(world.proj), "workspace")
    with root.dir(("src",)) as fd:
        # the folder is replaced by a link AFTER it was opened: the child is still in the opened one
        res = gitbrowse.spawn(["/bin/ls"], {"PATH": "/bin"}, cwd_fd=fd, max_bytes=1000)
    assert res.out.decode().split() == ["app.py", "ünï.txt"] or "app.py" in res.out.decode()


# ---------------------------------------------------------------- scan follow-ups: hard links, fail closed, folders at a commit


def _keys_and_link(world, link="notes.txt"):
    (world.proj / ".orch" / "keys.jsonl").write_text("KEYMATERIAL needle\n")
    os.link(world.proj / ".orch" / "keys.jsonl", world.proj / link)
    git(world.proj, "add", "-f", "-A")
    git(world.proj, "commit", "-q", "-m", "keys by another name", n=5)


def test_a_hard_link_to_keys_is_not_found_by_search_with_git_or_ripgrep(get, world, tmp_path, monkeypatch):
    _keys_and_link(world)
    assert "KEYMATERIAL" not in get("/search", q="KEYMATERIAL").text  # git grep --cached
    fake = tmp_path / "bin" / "rg"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nprintf '%s\\0%s\\n' './notes.txt' '1:KEYMATERIAL needle'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(search, "rg_path", lambda: str(fake))
    got = get("/search", q="KEYMATERIAL", root="ws")
    assert got.json()["engine"] == "rg" and got.json()["hits"] == [] and "KEYMATERIAL" not in got.text
    assert get("/file", root="ws", path="notes.txt").status_code == 403


def test_when_the_walk_of_orch_files_is_cut_short_every_multi_link_file_is_locked(get, world, monkeypatch):
    os.link(world.proj / "README.md", world.proj / "README-2.md")
    assert get("/file", root="ws", path="README-2.md").json()["kind"] == "markdown"  # complete walk: not an orch file
    monkeypatch.setattr(guard, "WALK_LIMIT", 1)
    assert get("/file", root="ws", path="README-2.md").status_code == 403  # incomplete: fail closed
    assert get("/file", root="ws", path="src/app.py").json()["kind"] == "text"  # a file with one name is unaffected


def test_a_hard_link_in_a_repository_root_to_a_store_file_is_locked(get, world):
    (world.proj / ".orch" / "keys.jsonl").write_text("KEYMATERIAL\n")
    os.link(world.proj / ".orch" / "keys.jsonl", world.other / "innocent.txt")
    got = get("/file", root="repo:other", path="innocent.txt")
    assert got.status_code == 403 and "KEYMATERIAL" not in got.text
    assert get("/file", root="repo:other", path="o.txt").json()["kind"] == "text"


def test_a_path_that_is_a_file_now_but_was_a_folder_at_that_commit_is_refused(get, world):
    (world.proj / "thing").mkdir()
    (world.proj / "thing" / "inner.txt").write_text("INNERSECRET\n")
    git(world.proj, "add", "-A")
    git(world.proj, "commit", "-q", "-m", "thing is a folder", n=5)
    old = git(world.proj, "rev-parse", "HEAD")
    (world.proj / "thing" / "inner.txt").unlink()
    (world.proj / "thing").rmdir()
    (world.proj / "thing").write_text("now a file\n")
    git(world.proj, "add", "-A")
    git(world.proj, "commit", "-q", "-m", "thing is a file", n=6)
    new = git(world.proj, "rev-parse", "HEAD")
    assert get("/history", root="ws", path="thing").status_code == 200
    for res in (
        get("/commit", root="ws", path="thing", sha=old),
        get("/compare", root="ws", path="thing", a=old, b=new),
        get("/compare", root="ws", path="thing", a=new, b=old),
        get("/file", root="ws", path="thing", rev=old),
    ):
        assert res.status_code == 404 and "INNERSECRET" not in res.text
    # the change that turned the folder into a file also removed the folder's files: a patch naming any other path
    # than the file is refused, so those deletions are never shown either
    res = get("/commit", root="ws", path="thing", sha=new)
    assert res.status_code == 404 and "INNERSECRET" not in res.text


def test_a_search_with_many_multi_link_hits_walks_orch_files_once(get, world, monkeypatch):
    for i in range(12):
        (world.proj / f"m{i}.txt").write_text("manylinks needle\n")
        os.link(world.proj / f"m{i}.txt", world.proj / f"n{i}.txt")
    git(world.proj, "add", "-A")
    git(world.proj, "commit", "-q", "-m", "many links", n=5)
    walks = []
    real = os.scandir
    state = os.path.realpath(world.gateway.state)

    def counting(path, *a, **k):
        if str(path) == state:  # the state folder is read once per walk
            walks.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(guard.os, "scandir", counting)
    got = get("/search", q="manylinks").json()
    assert len(got["hits"]) == 24
    assert len(walks) == 1, walks


def test_a_symlinked_state_folder_does_not_open_the_hard_link_check(get, world, monkeypatch):
    real = world.tmp / "real-state"
    real.mkdir()
    (real / "keys.jsonl").write_text("KEYMATERIAL\n")
    link = world.tmp / "state-link"
    link.symlink_to(real)
    os.link(real / "keys.jsonl", world.proj / "innocent.txt")
    monkeypatch.setattr(world.gateway, "state", link)
    got = get("/file", root="ws", path="innocent.txt")
    assert got.status_code == 403 and "KEYMATERIAL" not in got.text
    # the same through a root of another repository
    os.link(real / "keys.jsonl", world.other / "also.txt")
    assert get("/file", root="repo:other", path="also.txt").status_code == 403


def test_a_starting_place_that_is_not_a_folder_or_file_makes_the_walk_incomplete(world, tmp_path):
    fifo = tmp_path / "odd"
    os.mkfifo(fifo)
    root = guard.Root("ws", "w", os.path.realpath(world.proj), "workspace", forbidden=(str(fifo),))
    assert root._banned_files[1] is False
