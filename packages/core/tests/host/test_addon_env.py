"""V3 G4 steps 4 to 8 (design-g4 C1, C3, C4, C7): ``market.env_build`` end to end on a served workspace, with a fake
PyPI and npm behind the real fetcher, cache, pip and unpacker; the runner's check before every call; remove and update.
The adversarial cases of design section 5 are here: tampered lock, hash mismatch, sdist-only, postinstall, ``.pth``,
symlink out, network at run time."""

# ruff: noqa: F811
from __future__ import annotations

import functools
import json
import os
import socket
from types import SimpleNamespace

import pytest

from orch.addons import envtree, install
from orch.addons.runner import RunnerError
from orch.addons.runner import propose as _propose
from orch.host import addonenv
from orch.market import download
from tests.host import dep_helpers as D
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.dep_helpers import NODE, PY, dep_files, node_lock, py_lock
from tests.host.market_helpers import add_market, agent, err, ok, on_worker, person, stage

net = M.net
propose = functools.partial(_propose, gate=None)  # standalone: not about the hub policy
registry = D.registry
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PY_MAIN = b"""import json, sys
import idna

sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": idna.VALUE}}))
"""
NODE_MAIN = b"""const ms = require('ms');
process.stdin.once('data', () => {
  process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: 1, result: {value: ms}}) + '\\n');
});
"""
TICKET = SimpleNamespace(key="DEMO-0001", type="feature", status="open", visibility="workspace", fields={})


@pytest.fixture
def ready(served, net, registry):
    add_market(served, net)
    return served


def publish_py(served, net, registry, *, version="1.0.0", files=None, main=PY_MAIN, **kw):
    row = registry.py("idna", "3.10", files or {"idna/__init__.py": "VALUE = 'from-the-dependency'\n"}, **kw)
    pkg = dep_files("standup", version, py_lock(row), PY, main=main)
    net.add("standup", version, pkg=pkg, runtime=PY)
    net.version += 1
    ok(person(served, "market.refresh"))
    return row


def publish_node(served, net, registry, *, version="1.0.0", files=None, extra=None, main=NODE_MAIN):
    row = registry.node("ms", "2.1.3", files or {
        "package/package.json": b'{"name":"ms","version":"2.1.3","main":"index.js"}',
        "package/index.js": b"module.exports = 'node-dep-value';\n",
    }, extra)  # fmt: skip
    pkg = dep_files("standup", version, node_lock(row), NODE, main=main)
    net.add("standup", version, pkg=pkg, runtime=NODE)
    net.version += 1
    ok(person(served, "market.refresh"))
    return row


def build(served, name="standup"):
    return person(served, "market.env_build", {"name": name})


def location(served):
    s = served.service
    return envtree.EnvLocation(os.path.realpath(s.state_dir), s.workspace_id)


def granted_and_package(served, name="standup"):
    s = served.service
    on_worker(served, s.store.refresh)
    granted = install.as_addons(s.store.state.workspace.addons)[name]
    pkg, _ = install.read_installed(s.store.layout.store, name)
    return pkg, granted


def run_addon(served, name="standup"):
    pkg, granted = granted_and_package(served, name)
    return propose(pkg, granted, trigger="t", ticket=TICKET, env=location(served))


def env_path(served, name="standup"):
    _, granted = granted_and_package(served, name)
    return envtree.target_dir(location(served).state, location(served).ws, name, granted.package_sha256)


def build_state(served):
    return ok(person(served, "market.installed"))["data"]["addons"][0]["env"]


# ------------------------------------------------------------ happy paths
def test_python_install_build_and_a_call_that_uses_the_dependency(served, human, net, ready, registry):
    publish_py(served, net, registry)
    st, _, _ = M.install(served, human)
    assert build_state(served)["state"] == "needs_setup"  # signed, not built: never runs
    with pytest.raises(RunnerError) as e:
        run_addon(served)
    assert (e.value.code, e.value.detail) == ("market.env_changed", "missing")
    r = ok(build(served))["data"]
    assert (r["state"], r["count"] > 0) == ("ready", True)
    assert (env_path(served) / "idna/__init__.py").read_text().startswith("VALUE")
    env = build_state(served)
    assert (env["state"], env["kind"], env["count"] > 0, env["reason"]) == ("ready", "python-requirements", True, None)
    assert run_addon(served) == {"value": "from-the-dependency"}
    assert st["deps"]["count"] == 1
    assert [p for p in os.listdir(location(served).state + "/addon-env") if p.startswith(".build-")] == []


def test_node_install_build_and_a_call_that_uses_the_dependency(served, human, net, ready, registry, monkeypatch):
    publish_node(served, net, registry)
    M.install(served, human)
    r = ok(build(served))["data"]
    assert r["state"] == "ready"
    assert (env_path(served) / "node_modules/ms/index.js").exists()
    assert run_addon(served) == {"value": "node-dep-value"}


def test_an_agent_cannot_build_and_causes_no_download(served, human, net, ready, registry):
    publish_py(served, net, registry)
    M.install(served, human)
    n = len(registry.calls)
    assert err(agent(served, "market.env_build", {"name": "standup"})) == "market.actor_refused"
    assert len(registry.calls) == n and not env_path(served).exists()


def test_not_installed_and_a_name_without_dependencies(served, human, net, ready, registry):
    assert err(build(served, "nothing")) == "market.not_installed"
    net.add("standup", "1.0.0")
    net.version += 1
    ok(person(served, "market.refresh"))
    M.install(served, human)
    assert err(build(served)) == "market.not_installed"  # a grant without runtime has no environment
    assert build_state(served) is None


# ------------------------------------------------------------ the refusals at stage
def stage_refusal(served):
    r = stage(served)
    return r["error"]["code"], (r["error"].get("detail") or {}).get("reason")


def test_sdist_only_is_refused_at_stage_and_nothing_is_staged(served, net, ready, registry):
    row = registry.py("idna", "3.10", sdist=True)
    net.add("standup", "1.0.0", pkg=dep_files("standup", "1.0.0", py_lock(row), PY), runtime=PY)
    net.version += 1
    ok(person(served, "market.refresh"))
    assert stage_refusal(served) == ("market.env_failed", "sdist_only")
    assert M.mfiles(served).stages() == []


def test_a_hash_that_the_server_does_not_deliver_is_refused_at_stage(served, net, ready, registry):
    publish_py(served, net, registry)
    registry.corrupt("idna-3.10")
    assert stage_refusal(served) == ("market.env_failed", "hash_mismatch")
    assert os.listdir(location(served).state + "/addon-env/.dl") == []


def test_a_tampered_lock_is_a_digest_mismatch_and_with_a_matching_digest_a_hash_mismatch(served, net, ready, registry):
    row = registry.py("idna", "3.10")
    good = dep_files("standup", "1.0.0", py_lock(row), PY)
    evil = dep_files("standup", "1.0.0", py_lock((row[0], row[1], "e" * 64)), PY)
    net.add(
        "standup",
        "1.0.0",
        pkg=evil,
        runtime=PY,
        digest=__import__("orch.addons.package", fromlist=["x"]).package_digest(good),
    )
    net.version += 1
    ok(person(served, "market.refresh"))
    assert stage_refusal(served)[0] == "market.digest_mismatch"  # the lock was edited after the digest was published
    net.add("standup", "1.0.0", pkg=evil, runtime=PY)  # the publisher signs the edited lock: digest now matches
    net.version += 1
    ok(person(served, "market.refresh"))
    assert stage_refusal(served) == ("market.env_failed", "no_binary")  # no file of the index has that hash
    registry.projects["idna"][0]["hashes"]["sha256"] = "e" * 64  # an index that claims the edited hash...
    assert stage_refusal(served) == ("market.env_failed", "hash_mismatch")  # ...for other bytes


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"idna/__init__.py": "x\n", "evil.pth": "import os\n"}, "auto_run_file"),
        ({"idna/__init__.py": "x\n", "sitecustomize.py": "import os\n"}, "auto_run_file"),
        ({"idna/__init__.py": "x\n", "idna-3.10.data/data/sitecustomize.py": "import os\n"}, "auto_run_file"),
        ({"idna/__init__.py": "x\n", "idna-3.10.data/purelib/EVIL.PTH": "import os\n"}, "auto_run_file"),
        ({"idna/__init__.py": "x\n", "../outside.py": "x\n"}, "path_escape"),
    ],
)
def test_a_wheel_that_would_auto_run_or_escape_is_refused_at_stage(served, net, ready, registry, files, reason):
    publish_py(served, net, registry, files=files)
    assert stage_refusal(served) == ("market.env_failed", reason)


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"files": {"package/index.js": b"x", "package/node_modules/evil/index.js": b"x"}}, "package"),
        ({"extra": [("package/ln", {"type": __import__("tarfile").SYMTYPE, "linkname": "/etc/hosts"})]}, "link"),
        ({"files": {"package/../../evil": b"x"}}, "path_escape"),
    ],
)
def test_a_tarball_with_bundled_code_links_or_traversal_is_refused_at_stage(served, net, ready, registry, kw, reason):
    publish_node(served, net, registry, **kw)
    assert stage_refusal(served) == ("market.env_failed", reason)


# ------------------------------------------------------------ postinstall never runs
def test_a_dependency_install_script_never_runs(served, human, net, ready, registry, tmp_path):
    marker = tmp_path / "ran"
    pj = {
        "name": "ms", "version": "2.1.3", "main": "index.js",
        "scripts": {"postinstall": f"touch {marker}", "install": f"touch {marker}", "preinstall": f"touch {marker}"},
    }  # fmt: skip
    publish_node(served, net, registry, files={
        "package/package.json": json.dumps(pj).encode(), "package/index.js": b"module.exports = 'v';\n",
        "package/install.js": f"require('fs').writeFileSync({str(marker)!r}, '1')".encode(),
    })  # fmt: skip
    M.install(served, human)
    ok(build(served))
    assert run_addon(served) == {"value": "v"}
    assert not marker.exists()


# ------------------------------------------------------------ the check before every run
def built(served, human, net, registry, publisher=publish_py):
    publisher(served, net, registry)
    M.install(served, human)
    ok(build(served))
    return env_path(served)


def test_a_file_changed_added_or_replaced_by_a_link_after_the_build_stops_the_call(served, human, net, ready, registry):
    t = built(served, human, net, registry)
    assert run_addon(served)
    f = t / "idna/__init__.py"
    original = f.read_bytes()
    f.write_bytes(original + b"# edit\n")
    with pytest.raises(RunnerError) as e:
        run_addon(served)
    assert (e.value.code, e.value.detail) == ("market.env_changed", "changed")
    f.write_bytes(original)
    assert run_addon(served)
    (t / "sitecustomize.py").write_text("import os\n")  # planted at the root
    with pytest.raises(RunnerError):
        run_addon(served)
    (t / "sitecustomize.py").unlink()
    f.unlink()
    os.symlink("/etc/hosts", f)  # a symlink out of the environment
    with pytest.raises(RunnerError):
        run_addon(served)
    assert build_state(served)["state"] == "changed"
    # the owner repairs it with market.env_build (the target is removed and made again, never adopted)
    ok(build(served))
    assert run_addon(served) == {"value": "from-the-dependency"} and not f.is_symlink()


def test_the_environment_directory_replaced_by_a_link_is_not_followed(served, human, net, ready, registry, tmp_path):
    t = built(served, human, net, registry)
    copy = tmp_path / "copy"
    t.rename(copy)
    os.symlink(copy, t)
    with pytest.raises(RunnerError) as e:
        run_addon(served)
    assert e.value.detail == "missing"


def test_another_abi_stops_the_call(served, human, net, ready, registry, monkeypatch):
    built(served, human, net, registry)
    monkeypatch.setattr(envtree, "abi", lambda kind: "cp99|other")
    with pytest.raises(RunnerError) as e:
        run_addon(served)
    assert e.value.detail == "abi"


def test_no_network_is_touched_to_verify_or_to_run(served, human, net, ready, registry, monkeypatch):
    built(served, human, net, registry)
    pkg, granted = granted_and_package(served)

    def boom(*a, **k):
        raise AssertionError("the network was used at run time")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(download, "get", boom)
    monkeypatch.setattr(download.Cache, "fetch", boom)
    envtree.verify(location(served).state, location(served).ws, "standup", granted.package_sha256, PY["kind"])
    assert propose(pkg, granted, trigger="t", ticket=TICKET, env=location(served)) == {"value": "from-the-dependency"}


def test_runtime_and_env_must_agree_in_the_runner(served, human, net, ready, registry):
    built(served, human, net, registry)
    pkg, granted = granted_and_package(served)
    for bad in (None, {"kind": "node-package-lock", "lock": "package-lock.json", "packages": 1}):
        g = SimpleNamespace(**{**granted.__dict__, "runtime": bad})
        with pytest.raises(RunnerError) as e:
            propose(pkg, g, trigger="t", ticket=TICKET, env=location(served))
        assert e.value.code == "market.env_changed"
    with pytest.raises(RunnerError) as e:  # a missing env location for an add-on with dependencies
        propose(pkg, granted, trigger="t", ticket=TICKET)
    assert e.value.code == "market.env_changed"


# ------------------------------------------------------------ never adopted, superseded
def test_a_directory_planted_at_the_target_is_never_adopted(served, human, net, ready, registry):
    publish_py(served, net, registry)
    M.install(served, human)
    t = env_path(served)
    t.mkdir(parents=True, mode=0o700)
    (t / "evil.pth").write_text("import os\n")
    (t / "idna").mkdir()
    (t / "idna/__init__.py").write_text("VALUE = 'planted'\n")
    stamp = envtree.stamp_path(t)
    stamp.write_text("{}")
    ok(build(served))
    assert not (t / "evil.pth").exists()
    assert run_addon(served) == {"value": "from-the-dependency"}


def test_a_grant_that_changed_during_the_build_discards_it(served, human, net, ready, registry, monkeypatch):
    publish_py(served, net, registry)
    M.install(served, human)
    real = addonenv._normalize

    def disable_then_normalize(env):
        prepared = human.prepare("addon.disable", {"name": "standup"})
        ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
        real(env)

    monkeypatch.setattr(addonenv, "_normalize", disable_then_normalize)
    r = build(served)
    assert err(r) == "market.env_failed" and r["error"]["detail"] == {"reason": "superseded"}
    assert not env_path(served).exists()


# ------------------------------------------------------------ update and remove
def test_an_update_builds_a_new_digest_directory_and_removes_the_old_one(served, human, net, ready, registry):
    t1 = built(served, human, net, registry)
    row = registry.py("idna", "3.11", {"idna/__init__.py": "VALUE = 'v2'\n"})
    net.add("standup", "1.1.0", pkg=dep_files("standup", "1.1.0", py_lock(row), PY, main=PY_MAIN), runtime=PY)
    net.version += 1
    ok(person(served, "market.refresh"))
    M.install(served, human, update=True)
    with pytest.raises(RunnerError):  # signed, not built: the new version needs setup, it never runs unchecked
        run_addon(served)
    ok(build(served))
    t2 = env_path(served)
    assert t2 != t1 and not t1.exists() and not envtree.stamp_path(t1).exists()
    assert run_addon(served) == {"value": "v2"}


def test_disable_keeps_the_environment_and_uninstall_removes_it(served, human, net, ready, registry):
    t = built(served, human, net, registry)
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert t.exists()
    ok(person(served, "market.uninstall", {"name": "standup"}))
    assert not t.exists() and not t.parent.exists()


# ------------------------------------------------------------------------------------------------ review fix round
def test_a_wheel_that_zipfile_would_read_differently_is_refused_at_stage(served, net, ready, registry):
    """The zip64 polyglot of the Opus review: orch's directory and pip's differ; the lists must be identical."""
    from tests.market.test_review_g4_adversarial import polyglot, polyglot_slack

    for blob in (polyglot(), polyglot_slack()):
        row = registry.raw_wheel("idna", "3.10", blob)
        net.add("standup", "1.0.0", pkg=dep_files("standup", "1.0.0", py_lock(row), PY), runtime=PY)
        net.version += 1
        ok(person(served, "market.refresh"))
        r = stage(served)
        assert err(r) == "market.env_failed" and r["error"]["detail"] == {"reason": "package"}


def test_pip_runs_with_a_file_size_limit_and_no_config_files(served, human, net, ready, registry, monkeypatch):
    seen = {}
    real = addonenv.subprocess.Popen

    def spy(argv, **kw):
        seen["env"], seen["argv"] = kw["env"], argv
        return real(argv, **kw)

    monkeypatch.setattr(addonenv.subprocess, "Popen", spy)
    publish_py(served, net, registry)
    M.install(served, human)
    ok(build(served))
    assert seen["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert set(seen["env"]) == {"PATH", "HOME", "TMPDIR", "LANG", "PIP_CONFIG_FILE"}  # made from nothing
    assert "-c" in seen["argv"] and "RLIMIT_FSIZE" in addonenv._FSIZE_TRAMPOLINE and "--no-index" in seen["argv"]


def test_a_file_bigger_than_the_limit_stops_pip_and_the_build_fails_typed(
    served, human, net, ready, registry, monkeypatch
):
    publish_py(served, net, registry, files={"idna/__init__.py": "VALUE = 1\n", "idna/big.bin": "x" * 20000})
    M.install(served, human)
    monkeypatch.setattr(addonenv.untar, "MAX_FILE", 4096)  # pip may write no file over this
    r = build(served)
    assert err(r) == "market.env_failed" and r["error"]["detail"] == {"reason": "installer"}
    assert not env_path(served).exists()


def test_the_tree_size_watch_counts_without_following_links(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/f").write_bytes(b"x" * 100)
    os.symlink("/etc", tmp_path / "link")
    assert addonenv._tree_size(tmp_path, 1 << 30) == 100
    assert addonenv._tree_size(tmp_path, 10) > 10  # stops as soon as the limit is passed


def test_a_node_package_with_its_own_node_modules_is_refused_at_stage_and_by_the_runner(served, net, ready, registry):
    row = registry.node("ms", "2.1.3")
    pkg = dep_files("standup", "1.0.0", node_lock(row), NODE, main=NODE_MAIN)
    pkg["node_modules"] = b"a file with the name of the link"
    net.add("standup", "1.0.0", pkg=pkg, runtime=NODE)
    net.version += 1
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.package_invalid"


def test_the_node_modules_link_failing_is_a_typed_runner_error(tmp_path):
    from orch.addons import runner

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "node_modules").mkdir()
    with pytest.raises(RunnerError) as e:
        runner._link_node_modules(str(pkg), str(tmp_path / "env"))
    assert e.value.code == "addon.start_failed"


def test_the_stage_block_is_used_only_when_it_matches_the_lock_it_was_made_from():
    from orch.market import deps

    fresh = {
        "kind": "python-requirements", "lock": "requirements.txt", "count": 2, "bytes": 0,
        "rows": [
            {"name": "a", "version": "1", "installs": True, "note": None},
            {"name": "w", "version": "1", "installs": False, "note": "other_platform"},
        ],
    }  # fmt: skip
    import copy

    kept = copy.deepcopy(fresh)
    kept["bytes"] = 1234
    kept["rows"][0]["note"] = "compiled"  # the one thing the plan cannot know
    assert deps.consistent(kept, fresh)

    def bad(fn):
        k = copy.deepcopy(kept)
        fn(k)
        assert not deps.consistent(k, fresh)

    bad(lambda k: k["rows"][1].update(note="compiled"))  # not installed here, so it cannot be compiled
    bad(lambda k: k["rows"][1].update(note=None))  # a note the plan gives is not droppable
    bad(lambda k: k["rows"][0].update(note="dev"))
    bad(lambda k: k["rows"][0].update(version="2"))
    bad(lambda k: k["rows"][0].update(extra=1))
    bad(lambda k: k.update(bytes=-1))
    bad(lambda k: k.update(bytes=1 << 40))
    bad(lambda k: k.update(bytes="1"))
    bad(lambda k: k.update(count=3))
    bad(lambda k: k["rows"].pop())
    bad(lambda k: k.update(extra=1))
    assert not deps.consistent(None, fresh)


def test_a_runtime_grant_inside_a_batch_is_refused_by_the_signer():
    from orch.host import signer

    grant = {"type": "addon.granted", "runtime": {"kind": "python-requirements", "lock": "r.txt", "packages": 1}}
    with pytest.raises(signer.SignerRefused):
        signer._deps({}, [grant, {"type": "log.added"}])
    with pytest.raises(signer.SignerRefused):
        signer._deps({}, [{"type": "log.added"}, grant])
    signer._deps({}, [{"type": "addon.granted"}, {"type": "log.added"}])  # no runtime: nothing to require


# ------------------------------------------------------------------------------------------------ Q10: G6 {python}
def test_a_bare_python_is_refused_for_every_add_on_at_load_and_at_run():
    from orch.addons.manifest import ManifestError, load_manifest
    from tests.addons.test_manifest import raw, runtime_manifest

    for cmd0 in ("python3", "python", "python3.14", "Python3", "PYTHON", "python3.13t", "Python3.14T"):
        with pytest.raises(ManifestError):
            load_manifest(raw(runtime_manifest(cmd0=cmd0)))
        plain = {
            "schema": "orch.addon/3", "name": "p", "version": "1.0.0", "title": "P", "api": ">=1.1,<2",
            "entry": {"cmd": [cmd0, "{pkg}/m.py"]},
        }  # fmt: skip
        with pytest.raises(ManifestError):
            load_manifest(raw(plain))


def test_a_python_runtime_add_on_must_start_with_the_python_token_and_node_with_node():
    from orch.addons.manifest import ManifestError, load_manifest
    from tests.addons.test_manifest import raw, runtime_manifest

    load_manifest(raw(runtime_manifest(cmd0="{python}")))
    for cmd0 in ("node", "/usr/bin/python3"):
        with pytest.raises(ManifestError):
            load_manifest(raw(runtime_manifest(cmd0=cmd0)))


def test_the_runtime_interpreter_line_is_g6s_minus_dash_e(served, human, net, ready, registry, monkeypatch):
    from orch.addons import runner

    built(served, human, net, registry)
    seen = {}
    real = runner.subprocess.Popen

    def spy(argv, **kw):
        seen["argv"], seen["env"] = list(argv), kw["env"]
        return real(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    assert run_addon(served) == {"value": "from-the-dependency"}
    joined = " ".join(seen["argv"])
    assert " -B -s -S " in joined + " " and " -E " not in joined + " "  # kept -B -s -S, dropped -E
    path = seen["env"]["PYTHONPATH"].split(":")
    assert len(path) == 2 and path[1].endswith(str(env_path(served).name))  # <pkg>:<verified env>, from nothing
    assert set(seen["env"]) == {
        "PATH", "LANG", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "ORCH_ADDON",
        "ORCH_ADDON_CAPABILITIES",
    }  # fmt: skip


def test_a_dependency_import_fails_when_the_environment_is_not_verified(served, human, net, ready, registry):
    t = built(served, human, net, registry)
    (t / "idna/__init__.py").write_text("VALUE = 'evil'\n")
    with pytest.raises(RunnerError) as e:
        run_addon(served)  # verify runs inside the call: the add-on is never started
    assert (e.value.code, e.value.detail) == ("market.env_changed", "changed")


def test_a_non_runtime_add_on_keeps_g6s_full_line(tmp_path):
    from orch.addons import runner

    assert "-E" in runner.PYTHON_ARGV and {"-B", "-s", "-S"} <= set(runner.PYTHON_ARGV)
