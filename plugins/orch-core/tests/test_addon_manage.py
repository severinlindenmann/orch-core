import json
import subprocess

import pytest

from addon_fixtures import GOOD, make_addon
from orch.addons import manage, userfiles
from orch.addons.discovery import custom_addons_dir, find
from orch.errors import UsageError, ValidationError


@pytest.fixture(autouse=True)
def no_defaults(tmp_path, monkeypatch):
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")


@pytest.fixture(autouse=True)
def human_terminal(monkeypatch):
    """These tests call manage directly as the human at a terminal; test_admin_* below take that away."""
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)


def _git_repo(path):
    folder = make_addon(path)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "v1"]):
        subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)
    return folder


def test_install_from_a_path_is_untrusted(tmp_path):
    src = make_addon(tmp_path / "src")
    m = manage.install(str(src))
    target = custom_addons_dir() / "hello-status"
    assert m.name == "hello-status" and (target / "orch-addon.json").is_file()
    assert userfiles.registry_entries()["hello-status"]["source"] == {"kind": "path", "path": str(src.resolve())}
    assert userfiles.trust_state(find("hello-status")) == "untrusted"
    assert not any((custom_addons_dir() / ".staging").iterdir())


def test_install_refuses_bad_addons_and_cleans_up(tmp_path):
    src = make_addon(tmp_path / "src", extra={"hello_status/x.py": "import subprocess\n"})
    with pytest.raises(ValidationError, match="subprocess is not allowed"):
        manage.install(str(src))
    assert not (custom_addons_dir() / "hello-status").exists()
    assert not any((custom_addons_dir() / ".staging").iterdir())


def test_install_refuses_unsupported_api(tmp_path):
    with pytest.raises(ValidationError, match="requires_api '3' is not supported"):
        manage.install(str(make_addon(tmp_path / "src", manifest={**GOOD, "requires_api": "3"})))


def test_install_twice_and_default_names_are_refused(tmp_path, monkeypatch):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    with pytest.raises(ValidationError, match="already installed"):
        manage.install(str(src))
    from orch.addons import discovery
    defaults = tmp_path / "defaults"
    make_addon(defaults)
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: defaults)
    manage.remove("hello-status")
    with pytest.raises(ValidationError, match="default addon"):
        manage.install(str(src))


def test_parse_source():
    assert manage.parse_source("https://github.com/a/b.git", "v1")["kind"] == "git"
    with pytest.raises(UsageError):
        manage.parse_source("not-a-folder-or-url")


def test_install_from_git_records_the_commit(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    manage.install(f"file://{repo}")
    entry = userfiles.registry_entries()["hello-status"]
    assert entry["source"]["kind"] == "git" and len(entry["source"]["commit"]) == 40
    assert not (custom_addons_dir() / "hello-status" / ".git").exists()


def test_update_check_and_apply_from_a_path(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    manage.trust_addon("hello-status")
    assert [u.has_update for u in manage.update_check("hello-status")] == [False]
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0"}))
    info = manage.update_check("hello-status")[0]
    assert info.has_update and info.message == "update available 0.1.0 → 0.2.0"
    m = manage.update_apply("hello-status")
    assert m.version == "0.2.0"
    assert userfiles.trust_state(find("hello-status")) == "changed"  # disabled until re-trusted
    assert (custom_addons_dir() / ".previous" / "hello-status" / "orch-addon.json").is_file()


def test_update_check_from_git(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    manage.install(f"file://{repo}")
    assert not manage.update_check("hello-status")[0].has_update
    (repo / "README.md").write_text("# v2\n")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qam", "v2"], cwd=repo, check=True)
    assert manage.update_check("hello-status")[0].has_update


def test_rollback_restores_trust(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    manage.trust_addon("hello-status")
    (src / "README.md").write_text("# changed\n")
    manage.update_apply("hello-status")
    assert userfiles.trust_state(find("hello-status")) == "changed"
    manage.rollback("hello-status")
    assert userfiles.trust_state(find("hello-status")) == "trusted"
    assert (custom_addons_dir() / "hello-status" / "README.md").read_text() == "# Hello status\n"
    assert "previous" not in userfiles.registry_entries()["hello-status"]
    with pytest.raises(ValidationError, match="no previous version"):
        manage.rollback("hello-status")


def test_remove_drops_everything(tmp_path, ws):
    manage.install(str(make_addon(tmp_path / "src")))
    manage.trust_addon("hello-status")
    manage.enable(ws.root, "hello-status")
    manage.remove("hello-status")
    assert find("hello-status") is None and "hello-status" not in userfiles.registry_entries()
    assert userfiles.workspace_addons(ws.root) == {}


def test_review_lists_new_permissions_and_changed_files(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    first = manage.review("hello-status")
    assert first.old_version is None and first.added["binaries"] == ["git"]
    manage.trust_addon("hello-status", seen_digest=first.digest)
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0", "binaries": ["git", "gh"], "env": ["GH_HOST"]}))
    (src / "hello_status" / "new.py").write_text("X = 1\n")
    (src / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0\n\n- Adds gh.\n")
    manage.update_apply("hello-status")
    r = manage.review("hello-status")
    assert "## 0.2.0" in r.changelog
    assert r.old_version == "0.1.0" and r.version == "0.2.0"
    assert r.added == {"capabilities": [], "binaries": ["gh"], "env": ["GH_HOST"], "actions": [], "uploads": [], "remote_actions": []}
    assert "+ hello_status/new.py" in r.changed and "~ orch-addon.json" in r.changed


def test_trust_refuses_a_changed_or_failing_addon(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    with pytest.raises(ValidationError, match="changed since you reviewed it"):
        manage.trust_addon("hello-status", seen_digest="0" * 64)
    target = custom_addons_dir() / "hello-status" / "hello_status" / "provider.py"
    target.write_text(target.read_text().replace('return [Card("Hello", (Text(view.workspace_name),))]', "return ['<b>x</b>']"))
    with pytest.raises(ValidationError, match="fails the contract"):
        manage.trust_addon("hello-status")
    assert userfiles.trust_state(find("hello-status")) == "untrusted"


def test_enable_needs_trust(tmp_path, ws):
    manage.install(str(make_addon(tmp_path / "src")))
    with pytest.raises(ValidationError, match="trust it first"):
        manage.enable(ws.root, "hello-status")
    manage.trust_addon("hello-status")
    manage.enable(ws.root, "hello-status")
    assert userfiles.workspace_addons(ws.root)["hello-status"]["enabled"] is True
    manage.disable(ws.root, "hello-status")
    assert userfiles.workspace_addons(ws.root)["hello-status"]["enabled"] is False
    with pytest.raises(UsageError, match="not installed"):
        manage.enable(ws.root, "nope")


def test_install_refuses_symlinks(tmp_path):
    src = make_addon(tmp_path / "src")
    (src / "hello_status" / "linked.py").symlink_to(src / "hello_status" / "provider.py")
    with pytest.raises(ValidationError, match="symlinks are not allowed"):
        manage.install(str(src))
    assert not (custom_addons_dir() / "hello-status").exists()
    assert not any((custom_addons_dir() / ".staging").iterdir())


def test_trust_error_names_the_contract_problem(tmp_path):
    """Ruling F4: the child's `orch addon check --json` problem list reaches the trust error, not just an exit code."""
    manage.install(str(make_addon(tmp_path / "src")))
    target = custom_addons_dir() / "hello-status" / "hello_status" / "provider.py"
    target.write_text(target.read_text().replace('return [Card("Hello", (Text(view.workspace_name),))]', "return ['<b>x</b>']"))
    with pytest.raises(ValidationError) as err:
        manage.trust_addon("hello-status")
    assert "the contract run failed" not in err.value.message
    assert "widgets('page.hello-status')" in err.value.message


def test_run_contract_returns_the_problem_list(tmp_path):
    src = make_addon(tmp_path / "src", extra={"hello_status/x.py": "import subprocess\n"})
    problems = manage.run_contract(src)
    assert problems and all(isinstance(p, str) for p in problems)
    assert any("subprocess is not allowed" in p for p in problems)
    assert manage.run_contract(make_addon(tmp_path / "ok")) == []


def _commit_all(folder):
    for args in (["add", "-f", "."], ["-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "more"]):
        subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)


def test_sourceless_pyc_in_a_git_addon_is_refused_at_install(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "hello_status" / "evil.pyc").write_bytes(b"\0" * 32)
    _commit_all(repo)
    with pytest.raises(ValidationError, match="compiled files are not allowed"):
        manage.install(f"file://{repo}")
    assert not (custom_addons_dir() / "hello-status").exists()
    assert not any((custom_addons_dir() / ".staging").iterdir())


@pytest.mark.parametrize("name", ["evil.pyo", "evil.so", "evil.pyd", "evil.dylib", "evil.cpython-312-darwin.so"])
def test_compiled_files_are_refused_from_a_folder(tmp_path, name):
    src = make_addon(tmp_path / "src")
    (src / "hello_status" / name).write_bytes(b"\0")
    with pytest.raises(ValidationError, match="compiled files are not allowed"):
        manage.install(str(src))


def test_pycache_is_purged_after_clone(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "hello_status" / "__pycache__").mkdir()
    (repo / "hello_status" / "__pycache__" / "provider.cpython-312.pyc").write_bytes(b"\0" * 32)
    _commit_all(repo)
    manage.install(f"file://{repo}")
    assert not list((custom_addons_dir() / "hello-status").rglob("__pycache__"))


def _admin_calls(ws, src, who):
    kw = {} if who is None else {"actor": who}
    return {
        "install": lambda: manage.install(str(src), **kw),
        "update_apply": lambda: manage.update_apply("hello-status", **kw),
        "trust_addon": lambda: manage.trust_addon("hello-status", **kw),
        "enable": lambda: manage.enable(ws.root, "hello-status", **kw),
        "disable": lambda: manage.disable(ws.root, "hello-status", **kw),
        "rollback": lambda: manage.rollback("hello-status", **kw),
        "remove": lambda: manage.remove("hello-status", **kw),
    }


ADMIN_FUNCS = ["install", "update_apply", "trust_addon", "enable", "disable", "rollback", "remove"]


@pytest.mark.parametrize("func", ADMIN_FUNCS)
def test_admin_refused_inside_an_agent_harness_even_with_a_human_actor(tmp_path, ws, monkeypatch, func):
    from orch.core.events import Actor
    from orch.errors import HumanOnlyError
    src = make_addon(tmp_path / "src")
    if func != "install":
        manage.install(str(src))
    before = json.dumps(userfiles.registry_entries(), sort_keys=True)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    with pytest.raises(HumanOnlyError):
        _admin_calls(ws, src, Actor("human", "you", "dashboard"))[func]()
    assert json.dumps(userfiles.registry_entries(), sort_keys=True) == before
    assert userfiles.workspace_addons(ws.root) == {}


@pytest.mark.parametrize("func", ADMIN_FUNCS)
def test_admin_refused_without_a_terminal_or_with_an_agent_actor(tmp_path, ws, monkeypatch, func):
    from orch import actor
    from orch.core.events import Actor
    from orch.errors import HumanOnlyError
    src = make_addon(tmp_path / "src")
    if func != "install":
        manage.install(str(src))
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    with pytest.raises(HumanOnlyError):
        _admin_calls(ws, src, None)[func]()
    with pytest.raises(HumanOnlyError):
        _admin_calls(ws, src, Actor("agent", "x", "cli"))[func]()


def test_dashboard_human_actor_works_without_a_terminal(tmp_path, ws, monkeypatch):
    from orch import actor
    from orch.core.events import Actor
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    human = Actor("human", "you", "dashboard")
    manage.install(str(make_addon(tmp_path / "src")), actor=human)
    manage.trust_addon("hello-status", actor=human)
    manage.enable(ws.root, "hello-status", actor=human)
    assert userfiles.workspace_addons(ws.root)["hello-status"]["enabled"] is True


@pytest.mark.parametrize("text", ["--upload-pack=evil.git", "-uevil.git", "-x"])
def test_sources_starting_with_a_dash_are_refused(text):
    with pytest.raises(UsageError, match="must not start with -"):
        manage.parse_source(text)


def test_refs_starting_with_a_dash_are_refused():
    with pytest.raises(UsageError, match="must not start with -"):
        manage.parse_source("https://github.com/a/b.git", "--upload-pack=x")


def test_git_urls_come_after_a_double_dash(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    calls = []
    real = manage._git

    def spy(*args, **kw):
        calls.append(args)
        return real(*args, **kw)
    monkeypatch.setattr(manage, "_git", spy)
    url = f"file://{repo}"
    manage.install(url)
    manage.update_check("hello-status")
    clone = next(a for a in calls if a[0] == "clone")
    remote = next(a for a in calls if a[0] == "ls-remote")
    assert clone[clone.index("--") + 1] == url and remote[remote.index("--") + 1] == url


def test_rollback_restores_the_previous_versions_trust(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    manage.trust_addon("hello-status")
    v1 = userfiles.registry_entries()["hello-status"]["trusted_sha256"]
    (src / "README.md").write_text("# v2\n")
    manage.update_apply("hello-status")
    manage.trust_addon("hello-status")  # v2 trusted too
    assert userfiles.registry_entries()["hello-status"]["trusted_sha256"] != v1
    manage.rollback("hello-status")
    entry = userfiles.registry_entries()["hello-status"]
    assert entry["trusted_sha256"] == v1 and entry["trusted_version"] == "0.1.0"
    assert userfiles.trust_state(find("hello-status")) == "trusted"


def test_rollback_to_an_untrusted_version_is_untrusted(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    (src / "README.md").write_text("# v2\n")
    manage.update_apply("hello-status")
    manage.trust_addon("hello-status")
    manage.rollback("hello-status")
    assert userfiles.trust_state(find("hello-status")) == "untrusted"


def test_rollback_puts_the_current_version_back_if_the_swap_fails(tmp_path, monkeypatch):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    (src / "README.md").write_text("# v2\n")
    manage.update_apply("hello-status")
    from pathlib import Path
    real = Path.rename
    prev = custom_addons_dir() / ".previous" / "hello-status"

    def flaky(self, target):
        if Path(self) == prev:
            raise OSError("disk says no")
        return real(self, target)
    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError):
        manage.rollback("hello-status")
    assert (custom_addons_dir() / "hello-status" / "README.md").read_text() == "# v2\n"
    assert prev.is_dir()


def test_update_check_reports_a_failing_source_and_checks_the_rest(tmp_path):
    good = _git_repo(tmp_path / "good")
    manage.install(f"file://{good}")
    other = make_addon(tmp_path / "other", manifest={**GOOD, "name": "other-status"})
    manage.install(str(other))

    def mutate(data):
        data["hello-status"]["source"]["url"] = f"file://{tmp_path}/gone.git"
    userfiles.update_json(userfiles.addons_json_path(), mutate)
    infos = {i.name: i for i in manage.update_check()}
    assert set(infos) == {"hello-status", "other-status"}
    assert "check failed" in infos["hello-status"].message and not infos["hello-status"].has_update
    assert infos["hello-status"].failed and not infos["other-status"].failed


REMOTE = {"capabilities": [*GOOD["capabilities"], "decisions"], "remote_humans": True}


def test_review_flags_remote_humans_turning_on(tmp_path):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    first = manage.review("hello-status")
    assert first.remote_humans_added is False
    manage.trust_addon("hello-status", seen_digest=first.digest)
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, **REMOTE, "version": "0.2.0"}))
    manage.update_apply("hello-status")
    r = manage.review("hello-status")
    assert r.remote_humans_added is True and "decisions" in r.added["capabilities"]


def test_review_of_a_new_remote_humans_addon_flags_it(tmp_path):
    manage.install(str(make_addon(tmp_path / "src", manifest={**GOOD, **REMOTE})))
    assert manage.review("hello-status").remote_humans_added is True


def _monorepo(path):
    """A repository with the addon in addons/hello-status and server code at the root."""
    path.mkdir(parents=True)
    (path / "server.py").write_text("print(1)\n")
    make_addon(path / "addons")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "v1"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def test_install_from_a_folder_inside_a_git_repository(tmp_path):
    repo = _monorepo(tmp_path / "repo")
    m = manage.install(f"file://{repo}", subpath="addons/hello-status")
    target = custom_addons_dir() / m.name
    assert (target / "orch-addon.json").is_file() and not (target / "server.py").exists()
    assert not (target / ".git").exists() and not list((custom_addons_dir() / ".staging").glob("*.clone"))
    source = userfiles.registry_entries()[m.name]["source"]
    assert source["subpath"] == "addons/hello-status" and len(source["commit"]) == 40
    assert [u.has_update for u in manage.update_check(m.name)] == [False]


def test_update_keeps_the_folder_inside_the_repository(tmp_path):
    repo = _monorepo(tmp_path / "repo")
    m = manage.install(f"file://{repo}", subpath="./addons/hello-status/")
    manifest = repo / "addons" / "hello-status" / "orch-addon.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "version": "0.2.0"}))
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qam", "v2"], cwd=repo, check=True)
    assert manage.update_check(m.name)[0].has_update
    assert manage.update_apply(m.name).version == "0.2.0"
    assert not (custom_addons_dir() / m.name / "server.py").exists()


@pytest.mark.parametrize("bad", ["../x", "/etc", "addons/../../x", "", "C:/x"])
def test_path_must_stay_inside_the_repository(tmp_path, bad):
    with pytest.raises(UsageError):
        manage.parse_source("git@github.com:a/b.git", subpath=bad)


def test_path_is_only_for_git(tmp_path):
    src = make_addon(tmp_path / "src")
    with pytest.raises(UsageError):
        manage.install(str(src), subpath="addons/hello-status")


def test_path_without_an_addon_or_through_a_symlink_is_refused(tmp_path):
    repo = _monorepo(tmp_path / "repo")
    (repo / "elsewhere").symlink_to(repo / "addons" / "hello-status")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "link"], cwd=repo, check=True)
    for sub in ("addons", "missing", "elsewhere"):
        with pytest.raises(UsageError):
            manage.install(f"file://{repo}", subpath=sub)
    assert not list((custom_addons_dir() / ".staging").glob("*"))
