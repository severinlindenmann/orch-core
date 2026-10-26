"""H11e: the person-side records tool against a served workspace and local bare repos only (never a real remote).

The tie-break's acceptance: a historical ``.state/`` file blocks the push; a changed push URL is refused; the real
index is untouched; a tree with an extra file is refused; a non-fast-forward is never pushed."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

from orch.host import convenience
from orch.records import RecordsError
from orch.records import tool as rtool
from orch.records.cli import CHILD_SCHEMA, main
from orch.records.git import open_repo
from tests.host.live_helpers import world

WS_FILES = "file"


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private HOME for this test's own git calls (never the owner's), restored by the fixture."""
    path = tmp_path / "home"
    path.mkdir()
    (path / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.invalid\n")
    monkeypatch.setenv("ORCH_RECORDS_TEST_HOME", str(path))
    return path


def pinned_env() -> dict[str, str]:
    """The environment of this test's own git calls: HOME and the global config file pinned to the private HOME, the
    system config off, so nothing here passes or fails by the developer's ``~/.gitconfig`` (#541)."""
    home = os.environ["ORCH_RECORDS_TEST_HOME"]
    return {
        "HOME": home,
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.path.join(home, ".gitconfig"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
    }


def git(cwd, *args, env=None, check=True) -> str:
    e = pinned_env()
    e["LANG"] = "C"
    e.update(env or {})
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=e, check=False)
    if check and res.returncode != 0:
        raise AssertionError(res.stderr.decode())
    return res.stdout.decode().strip()


class Env:
    """One test's records world: the served workspace, a private HOME, local bare remotes and the host link."""

    def __init__(self, w, tmp_path: Path, home: Path) -> None:
        self.w = w
        self.root = Path(w.lab.root)
        self.store = self.root / ".orch"  # the records repository is the store
        self.home = home
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "XDG_CONFIG_HOME": str(self.home / ".config")}
        self.tips = rtool.ValidatedTips(tmp_path / "state" / "records.json")
        self.client = w.client()
        self.tmp = tmp_path
        self.calls: list[tuple[str, dict]] = []

    def link(self, op, args):
        self.calls.append((op, dict(args)))
        res = self.client.req(op, args, idem=op in ("records.remote_private", "records.note") or None)
        if not res["ok"]:
            raise RecordsError(res["error"]["code"], res["error"]["message"], exit_=res["exit"])
        return res["result"]["data"]

    def bare(self, name: str = "remote") -> str:
        path = self.tmp / f"{name}.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(path))
        return f"file://{path}"

    def repo(self, remote: str | None = "origin"):
        return open_repo(self.store, env=self.env, protocols=WS_FILES, remote=remote)

    def init(self) -> None:
        rtool.init_repo(self.store, env=self.env)

    def commit(self, **kw):
        return rtool.commit(self.repo(), self.link, self.w.wid, **kw)

    def push(self, **kw):
        return rtool.push(self.repo(), self.link, self.w.wid, self.tips, **kw)

    def cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        code = main(
            [*argv, "--json"], self.env, out, link=self.link, root=self.root, workspace_id=self.w.wid,
            tips=self.tips, protocols=WS_FILES, confirm=lambda text: True,
        )  # fmt: skip
        lines = out.getvalue().splitlines()
        assert len(lines) == 1, lines  # the child contract: exactly one JSON line
        doc = json.loads(lines[0])
        if argv[0] != "auto" or len(argv) > 1:
            jsonschema.validate(doc, CHILD_SCHEMA)
        return code, doc


@pytest.fixture
def r(w, tmp_path, home):
    return Env(w, tmp_path, home)


def records_message(wid: str, ws_seq: int = 1) -> str:
    """A commit message of exactly the records shape (``git commit -m`` keeps it, final newline included)."""
    return f"orch: records ws_seq {ws_seq}\n\nOrch-Manifest: m_{'0' * 64}\nOrch-Records: {wid}"


def tree_paths(root: Path, rev: str) -> list[str]:
    return git(root, "ls-tree", "-r", "--name-only", "--full-tree", rev).splitlines()


# --- commit -----------------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_commit_is_exactly_the_manifest_unsigned_with_the_trailer(r):
    r.w.ticket("one")
    r.init()
    res = r.commit()
    assert res["changed"] and res["index"] == "refreshed"
    paths = tree_paths(r.store, "HEAD")
    snap = r.link("records.snapshot", {})
    assert sorted(paths) == sorted(f["path"] for f in snap["files"])
    assert not any(p.startswith(".state") for p in paths)
    body = git(r.store, "cat-file", "commit", "HEAD")
    assert f"Orch-Records: {r.w.wid}" in body and f"Orch-Manifest: {res['manifest']}" in body
    assert "gpgsig" not in body and "author orch records <records@orch.invalid>" in body
    assert git(r.store, "status", "--porcelain") == ""  # the index was HEAD's: refreshed, nothing staged or untracked
    again = r.commit()
    assert again["changed"] is False and again["commit"] == res["commit"]
    r.w.ticket("two")
    second = r.commit()
    assert second["changed"] and git(r.store, "rev-parse", "HEAD^") == res["commit"]


def test_the_real_index_is_untouched_when_it_differs(r):
    r.init()
    r.commit()
    staged = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=r.store, input=b"staged by the person\n", capture_output=True,
        env=pinned_env(), check=True,
    ).stdout.decode().strip()  # fmt: skip
    git(r.store, "update-index", "--add", "--cacheinfo", f"100644,{staged},notes.txt")
    index = r.store / ".git" / "index"
    before = index.read_bytes()
    r.w.ticket("one")
    res = r.commit()
    assert res["changed"] and res["index"] == "differs"
    assert index.read_bytes() == before  # never written, not even a cache-tree
    assert "notes.txt" not in tree_paths(r.store, "HEAD")


def test_a_tree_with_an_extra_file_is_refused(r):
    r.init()
    repo = r.repo(None)
    manifest = rtool.fetch_manifest(r.link, r.w.wid)
    blobs = rtool.read_manifest_bytes(r.store, manifest)
    ids = rtool._store_blobs(repo, blobs)
    tree = rtool.build_tree(repo, manifest, ids)
    with rtool._temp_index() as index:  # the same entries plus one more
        repo.run("read-tree", tree, index=index)
        extra = next(iter(ids.values()))
        repo.run("update-index", "--add", "--cacheinfo", f"100644,{extra},.state/applied", index=index)
        bad = repo.out("write-tree", index=index)
    with pytest.raises(RecordsError) as e:
        rtool.verify_tree(repo, bad, ids)
    assert e.value.code == "records.tree_mismatch" and ".state/applied" in e.value.message


def test_commit_never_carries_an_old_non_record_file_forward(r):
    r.init()
    (r.store / ".state" / "secret").write_text("private\n")
    git(r.store, "add", "-f", ".state/secret", "config.json")
    git(r.store, "commit", "-qm", "by hand")
    res = r.commit()
    assert res["changed"]
    assert ".state/secret" not in tree_paths(r.store, "HEAD")  # the index was built from empty


def test_a_changed_snapshot_is_retried_once_then_refused(r, monkeypatch):
    r.init()
    calls = []
    monkeypatch.setattr(rtool, "stream_blobs", lambda repo, m: calls.append(m) and None)
    with pytest.raises(RecordsError) as e:
        r.commit()
    assert e.value.code == "records.snapshot_changed" and len(calls) == 2


def test_a_replaced_file_is_detected_by_its_hash(r):
    manifest = rtool.fetch_manifest(r.link, r.w.wid)
    (r.store / "config.json").write_bytes(b"x" * (r.store / "config.json").stat().st_size)
    assert rtool.read_manifest_bytes(r.store, manifest) is None


def test_mixed_pages_or_a_foreign_manifest_are_refused(r):
    def forged(op, args):
        data = r.link(op, args)
        data["files"] = data["files"][1:]
        return data

    with pytest.raises(RecordsError) as e:
        rtool.fetch_manifest(forged, r.w.wid)
    assert e.value.code == "records.snapshot_invalid"


# --- the repo ---------------------------------------------------------------------------------------------------------


def test_not_a_dedicated_repo_is_refused(r, tmp_path):
    with pytest.raises(RecordsError) as e:
        open_repo(r.store, env=r.env)
    assert e.value.code == "records.not_dedicated"


def test_merge_in_progress_and_detached_head_are_refused(r):
    r.init()
    r.commit()
    (r.store / ".git" / "MERGE_HEAD").write_text(git(r.store, "rev-parse", "HEAD") + "\n")
    with pytest.raises(RecordsError) as e:
        r.commit()
    assert e.value.code == "records.git_state"
    os.unlink(r.store / ".git" / "MERGE_HEAD")
    git(r.store, "checkout", "-q", "--detach")
    with pytest.raises(RecordsError) as e:
        r.commit()
    assert e.value.code == "records.git_state"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("url.https://evil.example/.insteadOf", "https://example.org/"),
        ("credential.helper", "!evil"),
        ("filter.x.clean", "evil"),
        ("core.sshCommand", "evil"),
        ("core.hooksPath", "/tmp"),
        ("http.proxy", "http://evil"),
        ("remote.origin.receivepack", "evil"),
        ("include.path", "/nonexistent/orch-records-include"),
    ],
)
def test_unsafe_local_config_is_refused_and_named(r, key, value):
    r.init()
    git(r.store, "remote", "add", "origin", r.bare())
    git(r.store, "config", "--local", key, value)
    with pytest.raises(RecordsError) as e:
        r.repo()
    assert e.value.code == "records.unsafe_config" and key.split(".")[0].lower() in e.value.message.lower()


def test_two_push_urls_and_disallowed_transports_are_refused(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    git(r.store, "remote", "set-url", "--add", "--push", "origin", r.bare("a"))
    git(r.store, "remote", "set-url", "--add", "--push", "origin", r.bare("b"))
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.remote_invalid"
    git(r.store, "remote", "remove", "origin")
    git(r.store, "remote", "add", "origin", "ext::sh -c touch% /tmp/pwned")
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.remote_invalid"
    git(r.store, "remote", "set-url", "origin", r.bare("c"))  # file:// is not allowed outside tests
    with pytest.raises(RecordsError) as e:
        rtool.push(open_repo(r.store, env=r.env, remote="origin"), r.link, r.w.wid, r.tips, initial=True)
    assert e.value.code == "records.remote_invalid"


# --- history ----------------------------------------------------------------------------------------------------------


def test_a_historical_state_file_blocks_the_push_and_a_fresh_history_unblocks(r):
    r.init()
    (r.store / ".state" / "secret").write_text("private\n")
    git(r.store, "add", "-f", ".state/secret")
    git(r.store, "commit", "-qm", records_message(r.w.wid))  # even with the exact records message
    git(r.store, "rm", "-q", "--cached", ".state/secret")
    git(r.store, "commit", "-qm", records_message(r.w.wid, 2))
    r.commit()  # HEAD's tree is clean, the history is not
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.history_invalid" and ".state/secret" in e.value.message
    assert not any(op == "records.export_check" for op, _ in r.calls)
    assert git(r.tmp, "--git-dir", url[len("file://") :], "for-each-ref") == ""  # nothing pushed
    res = r.commit(fresh=True)
    assert res["changed"] and git(r.store, "rev-list", "--count", "HEAD") == "1"
    pushed = r.push(initial=True)
    assert pushed["pushed"] and pushed["checked"] == 1


def test_a_foreign_commit_in_history_blocks_the_push(r):
    r.init()
    r.commit()
    git(r.store, "commit", "-q", "--allow-empty", "-m", "a note by hand")
    git(r.store, "remote", "add", "origin", r.bare())
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.not_records_only"


def test_grafts_and_shallow_are_refused(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    (r.store / ".git" / "shallow").write_text(git(r.store, "rev-parse", "HEAD") + "\n")
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.history_invalid"


# --- push -------------------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_push_initial_then_fast_forward_and_the_export_check_binds_s_url_ref(r):
    r.init()
    first = r.commit()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "records.push_rejected" and "--initial" in (e.value.hint or "")
    res = r.push(initial=True)
    assert res["pushed"] and res["commit"] == first["commit"] and res["url"] == url
    bare = url[len("file://") :]
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main") == first["commit"]
    assert git(r.tmp, "--git-dir", bare, "for-each-ref", "--format=%(refname)").splitlines() == ["refs/heads/main"]
    checks = [a for op, a in r.calls if op == "records.export_check"]
    want = {"sha": first["commit"], "url": url, "ref": "refs/heads/main", "history_convenience": False}
    assert checks == [want, want]  # before any network call (#441): the push refused for --initial was checked too
    r.w.ticket("one")
    second = r.commit()
    assert r.push()["from"] == first["commit"]
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main") == second["commit"]
    assert r.push() == {"pushed": False, "commit": second["commit"], "url": url, "ref": "refs/heads/main",
                        "reason": "up_to_date"}  # fmt: skip


@pytest.mark.slow
def test_a_non_fast_forward_is_never_pushed(r):
    r.init()
    r.commit()
    url = r.bare()
    bare = url[len("file://") :]
    git(r.store, "remote", "add", "origin", url)
    r.push(initial=True)
    other = r.tmp / "other"
    git(r.tmp, "clone", "-q", url, str(other))
    git(other, "commit", "-q", "--allow-empty", "-m", "foreign")
    git(other, "push", "-q", "origin", "main")
    foreign = git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main")
    r.w.ticket("one")
    r.commit()
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "records.push_rejected"
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main") == foreign  # untouched


@pytest.mark.slow
def test_the_lease_guards_the_race_without_authorizing_a_rewind(r, monkeypatch):
    r.init()
    first = r.commit()
    url = r.bare()
    bare = url[len("file://") :]
    git(r.store, "remote", "add", "origin", url)
    r.push(initial=True)
    r.w.ticket("one")
    second = r.commit()
    r.push()
    r.w.ticket("two")
    r.commit()
    monkeypatch.setattr(rtool, "remote_tip", lambda repo, u, ref: first["commit"])  # a stale ls-remote answer
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "records.push_rejected"
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main") == second["commit"]


def test_a_local_config_change_during_the_run_is_refused(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())

    def link(op, args):
        if op == "records.export_check":
            git(r.store, "config", "--local", "remote.origin.prune", "true")
        return r.link(op, args)

    with pytest.raises(RecordsError) as e:
        rtool.push(r.repo(), link, r.w.wid, r.tips, initial=True)
    assert e.value.code == "records.unsafe_config"


# --- the convenience-readers rule and the private-remote mark ---------------------------------------------------------


def _convenience(r):
    convenience.set_enabled(r.w.lab.state, r.w.wid, True, os.getuid())


@pytest.mark.slow
def test_push_with_convenience_needs_the_mark_for_the_exact_url(r):
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    _convenience(r)
    code, out = r.cli("push", "--initial")
    assert code == 3, out
    assert out["code"] == "sign.convenience_readers"
    assert git(r.tmp, "--git-dir", url_a[len("file://") :], "for-each-ref") == ""
    code, out = r.cli("remote-private", "--yes")
    assert code == 0 and out["detail"]["private_remote"]["url"] == url_a and "trusts your word" in out["detail"]["text"]
    code, out = r.cli("push", "--initial")
    assert code == 0 and out["pushed"] and out["detail"]["policy"] == "private_remote"
    # the destination changes: the remote URL
    git(r.store, "remote", "set-url", "origin", url_b)
    code, out = r.cli("push", "--initial")
    assert code == 3 and out["code"] == "sign.convenience_readers"
    assert git(r.tmp, "--git-dir", url_b[len("file://") :], "for-each-ref") == ""


def test_c15_the_dashboards_click_binds_the_address_it_showed(r):
    """#1198 C15: ``--expect-url`` is the address the person reviewed. Another address marks and pushes nothing, and
    the refusal does not echo either address."""
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    code, out = r.cli("remote-private", "--yes", "--expect-url", url_b)
    assert code == 3 and out["code"] == "records.remote_invalid"
    assert url_a not in json.dumps(out) and url_b not in json.dumps(out)
    assert not [a for op, a in r.calls if op == "records.remote_private"]  # the host was never asked
    code, out = r.cli("push", "--initial", "--yes", f"--expect-url={url_b}")
    assert code == 3 and out["code"] == "records.remote_invalid"
    assert git(r.tmp, "--git-dir", url_a[len("file://") :], "for-each-ref") == ""  # nothing was pushed
    code, out = r.cli("remote-private", "--yes", "--expect-url", url_a)
    assert code == 0 and out["detail"]["private_remote"]["url"] == url_a


@pytest.mark.slow
def test_a_pushurl_or_insteadof_change_after_marking_is_refused(r):
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    _convenience(r)
    assert r.cli("remote-private", "--yes")[0] == 0
    assert r.cli("push", "--initial")[1]["pushed"]
    r.w.ticket("one")
    r.commit()
    # a pushurl now sends the push elsewhere
    git(r.store, "config", "--local", "remote.origin.pushurl", url_b)
    code, out = r.cli("push", "--initial")
    assert out["code"] == "sign.convenience_readers"
    git(r.store, "config", "--local", "--unset", "remote.origin.pushurl")
    # a global insteadOf rewrite (the person's own config) redirects the marked URL
    with open(r.home / ".gitconfig", "a") as fh:
        fh.write(f'[url "{url_b}"]\n\tpushInsteadOf = {url_a}\n')
    code, out = r.cli("push", "--initial")
    assert out.get("code") == "sign.convenience_readers", out
    assert git(r.tmp, "--git-dir", url_b[len("file://") :], "for-each-ref") == ""
    checks = [a["url"] for op, a in r.calls if op == "records.export_check"]
    assert checks[-2:] == [url_b, url_b]  # the export check saw the effective destination, not the configured one


_CONV_LINE = json.dumps({"v": 2, "type": "log.added", "auth": "convenience", "text": "x"}, separators=(",", ":"))


def _forge_convenience_commit(
    r, path: str = "events/workspace.jsonl", line: str = _CONV_LINE, *, replace: bool = False
) -> str:
    """A records commit whose ``path`` carries ``line`` beyond what the store has (for an event log: a log file outside
    the merged view, as after an owner repair), made with plumbing only: the store is never touched."""
    env = {**pinned_env(), "LANG": "C", "GIT_INDEX_FILE": str(r.tmp / "forge.index")}
    have = b"" if replace else git(r.store, "show", f"HEAD:{path}", check=False).encode()
    data = (have + b"\n" if have else b"") + line.encode() + b"\n"
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=r.store, input=data, capture_output=True,
                          env=env, check=True).stdout.decode().strip()  # fmt: skip
    git(r.store, "read-tree", "HEAD", env=env)
    git(r.store, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
    tree = git(r.store, "write-tree", env=env)
    forged = git(r.store, "commit-tree", tree, "-p", "HEAD", "-m", records_message(r.w.wid, 9))
    git(r.store, "update-ref", "refs/heads/main", forged)
    return forged


@pytest.mark.slow
def test_a_convenience_event_in_a_pushed_log_outside_the_merged_view_is_refused(r, monkeypatch):
    from orch.ops.readers import seen_path

    r.init()
    r.commit()
    _forge_convenience_commit(r)
    res = r.commit()  # the store's real files on top: HEAD's tree is clean, the history is not
    assert res["changed"]
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)

    def no_network(*_a, **_k):
        raise AssertionError("a network call before the export check")

    monkeypatch.setattr(rtool, "remote_tip", no_network)
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "sign.convenience_readers"
    assert [a["history_convenience"] for op, a in r.calls if op == "records.export_check"] == [True]
    assert not seen_path(r.w.lab.state, r.w.wid).exists()  # the tool's report never writes the durable mark
    assert git(r.tmp, "--git-dir", url[len("file://") :], "for-each-ref") == ""
    assert r.tips.clean(r.w.wid, "main", rtool.SCANNER_VERSION) is None  # a dirty tip is never remembered as clean
    monkeypatch.undo()
    # the owner marks this exact URL private: allowed
    assert r.cli("remote-private", "--yes")[0] == 0
    assert r.cli("push", "--initial")[1]["pushed"]


@pytest.mark.slow
def test_restore_then_push_is_refused(r):
    """The merged history and the pushed history are clean now, but a convenience event once landed (the mark)."""
    from orch.ops.readers import ensure_convenience_mark

    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    ensure_convenience_mark(r.w.lab.state, r.w.wid, os.getuid())
    code, out = r.cli("push", "--initial")
    assert code == 3 and out["code"] == "sign.convenience_readers"


def test_a_scan_that_fails_pushes_nothing(r, monkeypatch):
    r.init()
    r.commit()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)

    def broken(*_a, **_k):
        raise RecordsError("records.git_failed", "git cat-file failed")

    monkeypatch.setattr(rtool, "history_has_convenience", broken)
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.git_failed"
    assert not any(op == "records.export_check" for op, _ in r.calls)
    assert git(r.tmp, "--git-dir", url[len("file://") :], "for-each-ref") == ""


def test_a_clean_tip_is_remembered_and_the_next_scan_covers_only_new_objects(r, monkeypatch):
    r.init()
    first = r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    assert r.push(initial=True)["pushed"]
    assert r.tips.clean(r.w.wid, "main", rtool.SCANNER_VERSION) == first["commit"]
    assert r.tips.clean(r.w.wid, "main", rtool.SCANNER_VERSION + 1) is None  # other rules: a full rescan
    seen: list = []
    real = rtool.history_has_convenience
    monkeypatch.setattr(rtool, "history_has_convenience", lambda repo, tip, since=None: seen.append(since) or real(
        repo, tip, since))  # fmt: skip
    r.w.ticket("one")
    r.commit()
    assert r.push()["pushed"]
    assert seen == [first["commit"]]


def test_the_scanner_reads_top_level_auth_only_and_fails_closed():
    has = rtool._blob_has_convenience  # noqa: SLF001
    assert has(b'{"a":1}\n{"auth":"convenience","type":"x"}\n')
    assert has(b'{"type":"x", "auth" : "convenience"}')
    assert has(b'{"\\u0061uth":"convenience"}\n')  # an escaped key
    assert has(b'{"auth":"\\u0063onvenience"}\n')  # an escaped value
    assert has(b'{"auth":"convenience","auth":"passphrase"}\n')  # a duplicate key never hides it
    assert not has(b'{"auth":"passphrase","text":"\\"auth\\":\\"convenience\\""}\n')  # quoted in a text
    assert not has(b'{"auth":"passphrase","signer":{"auth":"convenience"}}\n')  # not the event's own auth
    assert has(b'{"auth":"passphrase", torn\n')  # a line of an event log that does not parse counts
    assert has(b"[1, 2]\n")  # not an object
    assert has(b"\xff\xfe\n")  # not UTF-8
    assert has(b"[" * 100000 + b"\n")  # a nesting bomb: shaped, never an unhandled RecursionError
    assert not has(b"\n\n")


@pytest.mark.slow
@pytest.mark.parametrize("path", ["tickets/{uid}/body.md", "tickets/{uid}/artifacts/fixture.jsonl"])
def test_an_agents_body_or_artifact_quoting_a_convenience_line_is_never_counted(r, path):
    """Opus #454 M1, the PoC inverted: free text an agent writes is not an event log. Push allowed, no mark."""
    from orch.ops.readers import seen_path

    uid, _key = r.w.ticket("fixture")
    r.init()
    r.commit()
    _forge_convenience_commit(r, path.format(uid=uid), '  "auth": "convenience",')
    _forge_convenience_commit(r, path.format(uid=uid), _CONV_LINE)
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    res = r.push(initial=True)
    assert res["pushed"] and res["policy"] == "none"
    assert [a["history_convenience"] for op, a in r.calls if op == "records.export_check"] == [False]
    assert not seen_path(r.w.lab.state, r.w.wid).exists()


@pytest.mark.slow
@pytest.mark.parametrize("line", ['{"\\u0061uth":"convenience","v":2}', '{"auth":"\\u0063onvenience","v":2}'])
def test_an_escaped_convenience_event_in_a_pushed_log_is_refused(r, line):
    r.init()
    r.commit()
    _forge_convenience_commit(r, "events/workspace.jsonl", line)
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "sign.convenience_readers"


@pytest.mark.slow
def test_a_convenience_event_in_a_tickets_own_log_is_refused(r):
    uid, _key = r.w.ticket("one")
    r.init()
    r.commit()
    _forge_convenience_commit(r, f"tickets/{uid}/events.jsonl")
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "sign.convenience_readers"


@pytest.mark.slow
def test_an_old_blob_readded_at_an_event_log_path_after_a_clean_tip_is_refused(r):
    """The blob sat in a body (not an event log) when the tip was cached clean; re-added at an event-log path, the new
    commit names it there, so the incremental scan reads it."""
    uid, _key = r.w.ticket("one")
    r.init()
    r.commit()
    _forge_convenience_commit(r, f"tickets/{uid}/body.md", _CONV_LINE, replace=True)
    git(r.store, "remote", "add", "origin", r.bare())
    clean = r.push(initial=True)
    assert clean["pushed"] and r.tips.clean(r.w.wid, "main", rtool.SCANNER_VERSION) == clean["commit"]
    _forge_convenience_commit(r, "events/workspace.jsonl", _CONV_LINE, replace=True)  # the same blob, moved
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "sign.convenience_readers"


@pytest.mark.parametrize(
    "key", ["extensions.partialClone", "remote.origin.promisor", "remote.origin.partialclonefilter"]
)
def test_a_partial_clone_records_repo_is_refused(r, key):
    """Opus #454 I5: a backstop for git older than 2.45, where GIT_NO_LAZY_FETCH is ignored."""
    r.init()
    git(r.store, "remote", "add", "origin", r.bare())
    git(r.store, "config", "--local", key, "true" if "promisor" in key or "partial" in key.lower() else "x")
    with pytest.raises(RecordsError) as e:
        r.repo()
    assert e.value.code == "records.unsafe_config" and key.split(".")[-1].lower() in e.value.message.lower()


def test_the_records_git_never_lazy_fetches(r):
    r.init()
    assert r.repo().env()["GIT_NO_LAZY_FETCH"] == "1"


def test_without_convenience_push_needs_no_mark(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    code, out = r.cli("push", "--initial")
    assert code == 0 and out["pushed"] and out["detail"]["policy"] == "none"


# --- the CLI ----------------------------------------------------------------------------------------------------------


def test_cli_commit_and_init_and_agent_refusal(r):
    assert r.cli("init")[0] == 0
    code, out = r.cli("commit")
    assert code == 0 and out["detail"]["changed"]
    out_io = io.StringIO()
    code = main(["commit", "--json"], {**r.env, "ORCH_GRANT": "g.x"}, out_io, link=r.link, root=r.root,
                workspace_id=r.w.wid, tips=r.tips, protocols=WS_FILES)  # fmt: skip
    assert code == 3 and json.loads(out_io.getvalue())["code"] == "op.not_allowed"


def test_the_module_runs_isolated():
    res = subprocess.run(
        [os.sys.executable, "-I", "-m", "orch.records", "--help"], capture_output=True, check=False, timeout=60
    )
    assert res.returncode == 0 and b"commit" in res.stdout


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://example.org/me/r.git", True),
        ("ssh://git@example.org/me/r.git", True),
        ("git@example.org:me/r.git", True),
        ("example.org:/srv/r.git", True),
        ("ext::sh -c x", False),
        ("fd::3", False),
        ("file:///tmp/r.git", False),
        ("/tmp/r.git", False),
        ("http://example.org/r.git", False),
        ("git://example.org/r.git", False),
    ],
)
def test_transport_allow_list(url, ok):
    from orch.records.git import ALLOWED_PROTOCOLS, url_allowed

    assert url_allowed(url, ALLOWED_PROTOCOLS) is ok


# --- security scan findings (H11e commit scan) ------------------------------------------------------------------------


def test_finding1_caller_git_config_env_never_reaches_git(r):
    """GIT_CONFIG_* (or GIT_SSH_COMMAND, GIT_DIR) in the caller's environment cannot redirect the push."""
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    evil = {
        **r.env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{url_b}.pushInsteadOf",
        "GIT_CONFIG_VALUE_0": url_a,
        "GIT_CONFIG_PARAMETERS": f"'url.{url_b}.insteadof'='{url_a}'",
        "GIT_SSH_COMMAND": "touch /nonexistent/pwned",
        "GIT_DIR": str(r.tmp / "elsewhere"),
    }
    repo = open_repo(r.store, env=evil, protocols=WS_FILES, remote="origin")
    res = rtool.push(repo, r.link, r.w.wid, r.tips, initial=True)
    assert res["url"] == url_a
    assert git(r.tmp, "--git-dir", url_b[len("file://") :], "for-each-ref") == ""
    assert git(r.tmp, "--git-dir", url_a[len("file://") :], "rev-parse", "refs/heads/main") == res["commit"]


def test_finding1_global_config_is_frozen_between_check_and_push(r):
    """A ~/.gitconfig rewrite added after the repo was opened changes neither the checked nor the pushed URL."""
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    repo = r.repo()

    def link(op, args):
        if op == "records.export_check":
            with open(r.home / ".gitconfig", "a") as fh:
                fh.write(f'[url "{url_b}"]\n\tpushInsteadOf = {url_a}\n\tinsteadOf = {url_a}\n')
        return r.link(op, args)

    res = rtool.push(repo, link, r.w.wid, r.tips, initial=True)
    assert res["url"] == url_a and res["pushed"]
    assert git(r.tmp, "--git-dir", url_b[len("file://") :], "for-each-ref") == ""


# --- #541: of the person's global config, local calls see an allow-list only ----------------------------------------

_QUIET = {"GIT_CONFIG_GLOBAL": os.devnull}  # this test's own git calls once the hostile config is in place

#: programs a hostile ``~/.gitconfig`` makes plain git run for the operations the records tool performs (the positive
#: control below shows each one fires): ``core.fsmonitor`` (update-index, write-tree, diff-tree, diff-index),
#: ``core.hooksPath`` (reference-transaction on fast-import/update-ref; pre-push, and pre-receive/update/post-update on
#: a file:// remote's side), ``core.alternateRefsCommand`` (the receiving side of a push). Left out because the records
#: tool's operations never trigger them, even in plain git: filters (clean/smudge/process: no checkout, no add),
#: ``diff.external``/``diff.<d>.command``/textconv (only raw diffs), merge drivers (no merge), aliases (fixed argv),
#: ``core.pager``/``core.editor`` (no tty, ``-F -``) and ``gpg.program`` (``commit-tree`` does not sign by config).
_VECTORS = ("fsmonitor", "reference-transaction", "pre-push", "alternate-refs")
_HOOKS = ("reference-transaction", "pre-push", "pre-receive", "update", "post-update", "post-index-change")


def _hostile_gitconfig(r) -> Path:
    """Append to the private ``~/.gitconfig`` a program for every vector the records tool's operations can reach, each
    a link named after its vector to one script that appends that name to the returned marker; plus
    ``i18n.commitEncoding``, which would change the tool's own commits."""
    marker = r.tmp / "f541-marker"
    bin_ = r.tmp / "f541-bin"
    bin_.mkdir()
    evil = bin_ / "evil"
    evil.write_text(f'#!/bin/sh\nbasename "$0" >> "{marker}"\nexit 0\n')
    evil.chmod(0o755)
    hooks = r.tmp / "f541-hooks"
    hooks.mkdir()
    for name in (*_HOOKS, "fsmonitor", "alternate-refs"):
        ((hooks if name in _HOOKS else bin_) / name).symlink_to(evil)
    with open(r.home / ".gitconfig", "a") as fh:
        fh.write(
            f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {bin_ / 'fsmonitor'}\n"
            f"\talternateRefsCommand = {bin_ / 'alternate-refs'}\n[i18n]\n\tcommitEncoding = ISO-8859-1\n"
        )
    return marker


def _fired(marker: Path) -> set[str]:
    return set(marker.read_text().split()) if marker.exists() else set()


def test_541_local_calls_see_only_the_allow_listed_global_keys(r):
    """``Git.env`` is the one place: a local call gets ``user.*`` and ``url.*.(push)insteadOf`` of the person's global
    config, a call in the transport repo only the authentication and transport subset, nothing else (keys that name
    a program or change a read included, whether or not the tool's operations would run them today)."""
    r.init()
    git(r.store, "remote", "add", "origin", r.bare())
    _hostile_gitconfig(r)
    with open(r.home / ".gitconfig", "a") as fh:
        fh.write(
            "[core]\n\tattributesFile = /nonexistent/f541\n\tpager = f541\n\teditor = f541\n"
            '[filter "f541"]\n\tclean = f541\n\tsmudge = f541\n\tprocess = f541\n'
            '[diff]\n\texternal = f541\n[diff "f541"]\n\ttextconv = f541\n[merge "f541"]\n\tdriver = f541\n'
            "[gpg]\n\tprogram = f541\n[commit]\n\tgpgSign = true\n[alias]\n\tf541 = !f541\n"
            '[url "file:///nonexistent/f541/"]\n\tinsteadOf = https://f541.invalid/\n'
            "[credential]\n\thelper = f541-helper\n[http]\n\tsslVerify = true\n"
        )
    repo = r.repo()

    def keys(env):
        return {env[f"GIT_CONFIG_KEY_{i}"].lower() for i in range(int(env["GIT_CONFIG_COUNT"]))}

    def local_ok(key):  # a system gitconfig of the machine may add allow-listed keys: subset, not equality
        return key in ("user.name", "user.email") or (key.startswith("url.") and key.endswith(("insteadof",)))

    def net_ok(key):
        return key.startswith(("credential.", "http.", "ssh.")) or key == "core.sshcommand"

    local = keys(repo.env())
    assert all(local_ok(k) for k in local), local
    assert {"user.name", "user.email", "url.file:///nonexistent/f541/.insteadof"} <= local, local
    seen = repo.out("config", "--list").lower()
    for bad in ("filter.", "diff.", "merge.", "alias.", "core.attributesfile=/nonexistent", "core.alternaterefscommand",
                "core.pager", "core.editor", "gpg.", "commit.gpgsign", "i18n.commitencoding=iso", "credential.",
                "http.", "core.fsmonitor=/"):  # fmt: skip
        assert bad not in seen, bad
    assert "core.hookspath=/dev/null" in seen and f"core.hookspath={r.tmp}" not in seen
    with repo.transport() as net:
        for env in (net.env(network=True), net.env()):  # never url.* in the transport repo, network or not
            assert all(net_ok(k) for k in keys(env)) and {"credential.helper", "http.sslverify"} <= keys(env)
    assert "core.attributesfile=/dev/null" in seen  # not even git's default $XDG_CONFIG_HOME/git/attributes


def test_541_the_repos_own_commit_encoding_never_changes_a_records_commit(r):
    """``i18n.commitEncoding`` is allowed in the repo's own config (it runs nothing), but the records tool pins UTF-8:
    its commits stay records-only and the next push is not refused."""
    r.init()
    first = r.commit()["commit"]
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    git(r.store, "config", "--local", "i18n.commitEncoding", "ISO-8859-1")
    r.push(initial=True)
    r.w.ticket("one")
    second = r.commit()["commit"]
    assert "encoding " not in git(r.store, "cat-file", "commit", second)
    assert r.push()["from"] == first


def test_541_init_copies_no_template_at_all(r):
    """``records init`` runs ``git init --template=`` like the transport repo: no sample hooks, nothing of a template
    directory; the tool writes ``info/exclude`` itself."""
    r.init()
    assert not (r.store / ".git" / "hooks").exists()
    assert (r.store / ".git" / "info" / "exclude").read_text().startswith("# orch records")


def test_541_a_hostile_global_config_runs_nothing_and_changes_neither_the_commit_nor_the_ff_check(r):
    r.init()
    first = r.commit()["commit"]  # R
    url = r.bare()
    bare = url[len("file://") :]
    git(r.store, "remote", "add", "origin", url)
    r.push(initial=True)
    marker = _hostile_gitconfig(r)
    # positive control: plain git under the same HOME runs every vector for the records tool's own operations
    ctl, ctl_remote = r.tmp / "f541-ctl", r.tmp / "f541-ctl.git"
    git(r.tmp, "init", "-q", "--bare", "-b", "main", str(ctl_remote), env=_QUIET)
    git(r.tmp, "init", "-q", "-b", "main", str(ctl), env=_QUIET)
    (r.tmp / "f541-blob").write_text("x\n")
    blob = git(ctl, "hash-object", "-w", str(r.tmp / "f541-blob"))
    git(ctl, "update-index", "--add", "--cacheinfo", f"100644,{blob},x.txt")
    tree = git(ctl, "write-tree")
    commit = git(ctl, "commit-tree", "--no-gpg-sign", tree, "-m", "control")
    assert "\nencoding ISO-8859-1\n" in git(ctl, "cat-file", "commit", commit)
    git(ctl, "update-ref", "refs/heads/main", commit)
    git(ctl, "push", "-q", f"file://{ctl_remote}", "main")
    assert _fired(marker) >= set(_VECTORS), _fired(marker)
    marker.unlink()
    # the records tool under that config: nothing runs, its commits and the fast-forward check are unchanged
    r.w.ticket("one")
    t = r.commit()["commit"]  # T, child of R
    assert "encoding " not in git(r.store, "cat-file", "commit", t, env=_QUIET)
    assert r.push()["from"] == first  # a fast-forward is still one
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main", env=_QUIET) == t
    git(r.store, "update-ref", "refs/heads/main", first, t, env=_QUIET)  # back to R, then a sibling S of T
    git(r.store, "read-tree", first, env=_QUIET)
    r.w.ticket("two")
    s = r.commit()["commit"]
    assert git(r.store, "rev-parse", f"{s}^", env=_QUIET) == first
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "records.push_rejected" and "never a non-fast-forward" in e.value.message
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main", env=_QUIET) == t
    code, out = r.cli("status")  # its diff-index runs under the hostile config too
    assert code == 0 and out["detail"]["status"]["local"] == {"repo": {"state": "ok"}, "url": url}, out
    assert _fired(marker) == set()


def test_541_init_reads_no_template_or_other_config_of_the_person(r):
    """``orch records init`` runs ``git init`` with the system and global config off: a person's ``init.templateDir``
    (hooks, a ``config`` file) is never copied into the records repo."""
    tpl = r.tmp / "f541-template"
    (tpl / "hooks").mkdir(parents=True)
    (tpl / "f541-from-template").write_text("x\n")
    (tpl / "config").write_text("[core]\n\tfsmonitor = /nonexistent/f541\n")
    with open(r.home / ".gitconfig", "a") as fh:
        fh.write(f"[init]\n\ttemplateDir = {tpl}\n")
    r.init()
    assert not (r.store / ".git" / "f541-from-template").exists()
    assert "f541" not in (r.store / ".git" / "config").read_text()
    r.commit()


def test_finding2_a_url_with_a_password_is_refused_and_never_echoed(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", "https://me:s3cret-token@example.org/r.git")
    code, out = r.cli("push", "--initial")
    assert out["code"] == "records.remote_invalid"
    assert "s3cret" not in json.dumps(out)
    assert not any(op == "records.export_check" for op, _ in r.calls)


def test_finding2_git_errors_are_redacted():
    from orch.records.git import redact

    assert redact("fatal: unable to access 'https://me:tok@example.org/r.git/'") == (
        "fatal: unable to access 'https://***@example.org/r.git/'"
    )
    assert redact("a\x1b[31mred") == "a?[31mred"


def test_finding3_a_symlinked_record_root_path_is_refused(r, tmp_path):
    r.init()
    link_path = tmp_path / "ws-link"
    os.symlink(r.store, link_path)
    with pytest.raises(RecordsError) as e:
        open_repo(link_path, env=r.env)
    assert e.value.code == "records.unsafe_tree"


def test_finding3_the_real_index_is_never_written_through_a_symlink(r, tmp_path):
    r.init()
    r.commit()
    target = tmp_path / "victim"
    target.write_bytes(b"do not touch\n")
    index = r.store / ".git" / "index"
    os.unlink(index)
    os.symlink(target, index)
    r.w.ticket("one")
    with pytest.raises(RecordsError) as e:
        r.commit()
    assert e.value.code == "records.unsafe_tree"
    assert target.read_bytes() == b"do not touch\n"


def test_finding3_a_planted_index_lock_symlink_is_never_followed(r, tmp_path):
    r.init()
    r.commit()
    target = tmp_path / "victim"
    target.write_bytes(b"do not touch\n")
    os.symlink(target, r.store / ".git" / "index.lock")
    r.w.ticket("one")
    res = r.commit()
    assert res["changed"] and res["index"] == "differs"
    assert target.read_bytes() == b"do not touch\n"
    assert os.path.islink(r.store / ".git" / "index.lock")


def test_finding3_init_never_writes_exclude_through_a_symlink(r, tmp_path):
    r.init()
    target = tmp_path / "victim"
    target.write_bytes(b"do not touch\n")
    exclude = r.store / ".git" / "info" / "exclude"
    os.unlink(exclude)
    os.symlink(target, exclude)
    with pytest.raises(RecordsError):
        r.init()
    assert target.read_bytes() == b"do not touch\n"


def test_finding4_file_count_and_gitfile_reads_are_bounded(r, w, monkeypatch, tmp_path):
    from orch import records as rec
    from orch.records.git import git_dir_of

    monkeypatch.setattr(rec, "FILES_MAX", 1)
    w.ticket("one")
    res = w.client().req("records.snapshot", {})
    assert res["ok"] is False and res["error"]["code"] == "records.too_large"
    d = tmp_path / "gitfile"
    d.mkdir()
    (d / ".git").write_bytes(b"gitdir: " + b"x" * (1 << 20))
    with pytest.raises(RecordsError) as e:
        git_dir_of(d)
    assert e.value.code == "records.not_dedicated"


@pytest.mark.parametrize(
    "url",
    ["https://ghp_TOKEN1234567890@example.org/r.git", "https://me:ghp_TOKEN1234567890@example.org/r.git",
     "ssh://me:ghp_TOKEN1234567890@example.org/r.git"],
)  # fmt: skip
def test_finding5_a_token_bearing_url_appears_nowhere(r, url):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", url)
    _convenience(r)
    outputs = [r.cli("push", "--initial")[1], r.cli("remote-private", "--yes")[1]]
    for out in outputs:
        assert out["code"] == "records.remote_invalid"
    # the host refuses it too, without echoing it
    for op, args in (
        ("records.remote_private", {"url": url, "on": True}),
        ("records.export_check", {"sha": "a" * 40, "url": url, "ref": "refs/heads/main", "history_convenience": False}),
    ):
        res = r.client.req(op, args)
        assert res["ok"] is False and res["error"]["code"] == "invalid.input"
        outputs.append(res)
    assert not any(op in ("records.export_check", "records.remote_private") for op, _ in r.calls)
    blob = json.dumps(outputs)
    for path in (r.w.lab.state, r.tmp / "state"):
        for dirpath, _dirs, files in os.walk(path):
            for name in files:
                with contextlib.suppress(OSError):
                    blob += open(os.path.join(dirpath, name), "rb").read().decode(errors="replace")
    blob += git(r.store, "log", "--all", "--format=%B")
    assert "TOKEN1234567890" not in blob


def test_finding5_redaction_covers_token_only_userinfo():
    from orch.records.git import has_credentials, redact

    assert "TOK" not in redact("fatal: could not read from https://TOK@example.org/r.git")
    assert has_credentials("https://TOK@example.org/r.git") and has_credentials("ssh://a:b@h/r")
    assert not has_credentials("ssh://git@example.org/r.git") and not has_credentials("git@example.org:r.git")


# --- Opus #430 review round -------------------------------------------------------------------------------------------


def test_opus1_a_remote_named_after_the_marked_url_never_redirects_the_push(r):
    """Opus's repro: ``remote."<marked URL>".pushurl = B`` in the repo's config. Refused, and B receives nothing."""
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    _convenience(r)
    assert r.cli("remote-private", "--yes")[0] == 0
    git(r.store, "config", "--local", f"remote.{url_a}.pushurl", url_b)
    code, out = r.cli("push", "--initial")
    assert code == 3 and out["code"] == "records.unsafe_config"
    for url in (url_a, url_b):
        assert git(r.tmp, "--git-dir", url[len("file://") :], "for-each-ref") == ""


def test_opus1_a_global_remote_entry_for_the_url_never_reaches_the_network_calls(r):
    """The same through the person's frozen global config: the transport repo keeps no ``remote.*``, so the push
    goes to the checked URL A and B receives nothing."""
    r.init()
    r.commit()
    url_a, url_b = r.bare("a"), r.bare("b")
    git(r.store, "remote", "add", "origin", url_a)
    _convenience(r)
    assert r.cli("remote-private", "--yes")[0] == 0
    with open(r.home / ".gitconfig", "a") as fh:
        fh.write(f'[remote "{url_a}"]\n\tpushurl = {url_b}\n\treceivepack = touch /nonexistent/pwned\n')
    code, out = r.cli("push", "--initial")
    assert code == 0 and out["pushed"] and out["url"] == url_a
    assert git(r.tmp, "--git-dir", url_b[len("file://") :], "for-each-ref") == ""
    assert git(r.tmp, "--git-dir", url_a[len("file://") :], "rev-parse", "refs/heads/main") == out["commit"]


def test_opus1_any_other_remote_section_in_the_repo_config_is_refused(r):
    r.init()
    git(r.store, "remote", "add", "origin", r.bare())
    git(r.store, "remote", "add", "backup", r.bare("backup"))
    with pytest.raises(RecordsError) as e:
        r.repo("origin")
    assert e.value.code == "records.unsafe_config" and "remote.backup" in e.value.message
    r.repo(None)  # a commit does not push: other remotes do not matter there


def _forge_commit_graph(repo_root: Path, child: str, fake_parent: str) -> None:
    """Write a commit-graph (generation v1) and patch ``child``'s first parent to ``fake_parent`` (Opus's repro)."""
    commits = git(repo_root, "rev-list", "--all", child, fake_parent).split()
    subprocess.run(
        ["git", "-c", "commitGraph.generationVersion=1", "commit-graph", "write", "--stdin-commits"],
        cwd=repo_root, input="\n".join(commits).encode(), capture_output=True, check=True,
        env=pinned_env(),
    )  # fmt: skip
    path = repo_root / ".git" / "objects" / "info" / "commit-graph"
    data = bytearray(path.read_bytes())
    assert data[:4] == b"CGPH"
    nchunks = data[6]
    chunks = {}
    for i in range(nchunks + 1):
        off = 8 + 12 * i
        chunks[bytes(data[off : off + 4])] = int.from_bytes(data[off + 4 : off + 12], "big")
    oidl, cdat = chunks[b"OIDL"], chunks[b"CDAT"]
    n = (cdat - oidl) // 20
    oids = [bytes(data[oidl + 20 * i : oidl + 20 * (i + 1)]).hex() for i in range(n)]
    ci, pi = oids.index(child), oids.index(fake_parent)
    rec_c, rec_p = cdat + 36 * ci, cdat + 36 * pi
    data[rec_c + 20 : rec_c + 24] = pi.to_bytes(4, "big")
    gen_p = int.from_bytes(data[rec_p + 28 : rec_p + 32], "big") >> 2
    word = int.from_bytes(data[rec_c + 28 : rec_c + 32], "big")
    data[rec_c + 28 : rec_c + 32] = (((gen_p + 1) << 2) | (word & 3)).to_bytes(4, "big")
    os.chmod(path, 0o644)
    path.write_bytes(bytes(data))


def test_opus2_a_forged_commit_graph_never_lets_a_non_fast_forward_through(r):
    r.init()
    root_commit = r.commit()["commit"]  # R
    url = r.bare()
    bare = url[len("file://") :]
    git(r.store, "remote", "add", "origin", url)
    r.push(initial=True)
    r.w.ticket("one")
    t = r.commit()["commit"]  # T, child of R
    r.push()
    git(r.store, "update-ref", "refs/heads/main", root_commit, t)  # back to R, then a sibling S of T
    git(r.store, "read-tree", root_commit)
    r.w.ticket("two")
    s = r.commit()["commit"]
    assert git(r.store, "rev-parse", f"{s}^") == root_commit
    _forge_commit_graph(r.store, s, t)
    # the forgery works on plain git: with the graph, T looks like an ancestor of S
    plain = subprocess.run(
        ["git", "merge-base", "--is-ancestor", t, s], cwd=r.store, capture_output=True, check=False,
        env=pinned_env(),
    )  # fmt: skip
    assert plain.returncode == 0
    with pytest.raises(RecordsError) as e:
        r.push()
    assert e.value.code == "records.push_rejected" and "never a non-fast-forward" in e.value.message
    assert git(r.tmp, "--git-dir", bare, "rev-parse", "refs/heads/main") == t


def test_opus5_extra_headers_or_another_message_are_not_records_commits(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", r.bare())
    git(r.store, "-c", "i18n.commitEncoding=ISO-8859-1", "commit", "-q", "--allow-empty", "-m",
        records_message(r.w.wid, 5))  # fmt: skip
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.not_records_only" and "headers" in e.value.message
    git(r.store, "reset", "-q", "--soft", "HEAD^")
    git(r.store, "commit", "-q", "--allow-empty", "-m", records_message(r.w.wid, 5) + "\n\nexported note")
    with pytest.raises(RecordsError) as e:
        r.push(initial=True)
    assert e.value.code == "records.not_records_only"


def test_opus6_a_push_url_with_a_query_or_fragment_is_refused(r):
    r.init()
    r.commit()
    git(r.store, "remote", "add", "origin", "https://example.org/r.git?token=SECRET123")
    code, out = r.cli("push", "--initial")
    assert out["code"] == "records.remote_invalid" and "SECRET123" not in json.dumps(out)


def test_opus6_open_root_errors_carry_no_path(tmp_path):
    from orch.records import open_root

    with pytest.raises(RecordsError) as e:
        open_root(tmp_path / "nowhere-secret-name")
    assert "nowhere-secret-name" not in e.value.message and str(tmp_path) not in e.value.message


# --- Sonnet #430 nits -------------------------------------------------------------------------------------------------


def test_a_corrupt_state_file_is_unsafe_config(r):
    r.tips.path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(r.tips.path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"{not json")
    os.close(fd)
    with pytest.raises(RecordsError) as e:
        r.tips.get(r.w.wid, "main")
    assert e.value.code == "records.unsafe_config"


def test_a_malformed_or_endless_host_reply_is_snapshot_invalid(r):
    for reply in ({"manifest": 1}, {"nothing": True}, None, {"manifest": "m", "heads": [], "ws_seq": 1}):
        with pytest.raises(RecordsError) as e:
            rtool.fetch_manifest(lambda op, args, reply=reply: reply, r.w.wid)
        assert e.value.code == "records.snapshot_invalid"
    endless = {"manifest": "m_x", "heads": {}, "ws_seq": 1, "workspace": r.w.wid, "files": [], "next_page": "p"}
    with pytest.raises(RecordsError) as e:
        rtool.fetch_manifest(lambda op, args: endless, r.w.wid)
    assert e.value.code == "records.snapshot_invalid" and "pages" in e.value.message


def test_integrity_refusals_exit_3():
    for code in ("records.tree_mismatch", "records.git_failed", "records.history_invalid", "records.push_rejected"):
        assert RecordsError(code, "x").exit == 3
