import json
import os
import sys
import threading

import pytest

from addon_fixtures import GOOD
from orch.addons import userfiles
from orch.addons.discovery import Found
from orch.addons.manifest import parse_manifest
from orch.errors import ValidationError


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("ORCH_STATE_DIR", str(d))
    return d


def _custom(base, name="hello-status"):
    folder = base / "addons" / name
    (folder / "hello_status").mkdir(parents=True)
    (folder / "orch-addon.json").write_text(json.dumps({**GOOD, "name": name}), encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text("def create(ctx):\n    return object()\n", encoding="utf-8")
    return Found(name, "custom", folder, parse_manifest({**GOOD, "name": name}))


def test_paths_follow_the_orch_config_dir(state):
    assert userfiles.addons_json_path() == state / "addons.json"
    assert userfiles.workspaces_json_path() == state / "workspaces.json"


def test_enable_and_config_round_trip(state, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    userfiles.set_enabled(root, "hello-status", True)
    userfiles.save_addon_config(root, "hello-status", {"greeting": "Hi"})
    assert userfiles.workspace_addons(root) == {"hello-status": {"enabled": True, "config": {"greeting": "Hi"}, "background": False}}
    userfiles.set_enabled(root, "hello-status", False)
    assert userfiles.workspace_addons(root)["hello-status"] == {"enabled": False, "config": {"greeting": "Hi"}, "background": False}


def test_register_keeps_the_addons_section(state, ws):
    from orch.dashboard import switcher
    userfiles.set_enabled(ws.root, "hello-status", True)
    switcher.register(ws, 8765)
    entry = json.loads((state / "workspaces.json").read_text())[userfiles.workspace_key(ws.root)]
    assert entry["last_port"] == 8765 and entry["addons"]["hello-status"]["enabled"] is True


def test_enable_keeps_switcher_fields(state, ws):
    from orch.dashboard import switcher
    switcher.register(ws, 8766)
    userfiles.set_enabled(ws.root, "hello-status", True)
    entry = json.loads((state / "workspaces.json").read_text())[userfiles.workspace_key(ws.root)]
    assert entry["last_port"] == 8766 and entry["name"] == "acme" and entry["addons"]["hello-status"]["enabled"] is True


def test_other_workspaces_are_untouched(state, tmp_path):
    other = {"path": "/elsewhere", "name": "northwind", "last_port": 9000}
    state.mkdir(parents=True)
    (state / "workspaces.json").write_text(json.dumps({"/elsewhere": other}))
    userfiles.set_enabled(tmp_path, "x", True)
    assert json.loads((state / "workspaces.json").read_text())["/elsewhere"] == other


@pytest.mark.parametrize("text", ["{not json", "[]", '{"K": {"addons": []}}', '{"K": {"addons": {"x": {"enabled": "yes"}}}}',
                                  '{"K": {"addons": {"Bad/Name": {"enabled": true}}}}'])
def test_corrupt_files_load_nothing_and_keep_a_copy(state, tmp_path, text):
    root = tmp_path / "ws"
    root.mkdir()
    state.mkdir(parents=True)
    (state / "workspaces.json").write_text(text.replace('"K"', json.dumps(userfiles.workspace_key(root))))
    assert all(not v["enabled"] for v in userfiles.workspace_addons(root).values())
    (state / "addons.json").write_text(text)
    assert isinstance(userfiles.registry_entries(), dict)
    userfiles.set_enabled(root, "hello-status", True)
    assert userfiles.workspace_addons(root)["hello-status"]["enabled"] is True
    if text in ("{not json", "[]"):
        assert (state / "workspaces.json.broken").read_text() == text


def test_concurrent_writers_do_not_lose_updates(state, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    threads = [threading.Thread(target=userfiles.set_enabled, args=(root, f"a{i}", True)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(userfiles.workspace_addons(root)) == sorted(f"a{i}" for i in range(8))


def test_tree_hash_ignores_caches_and_sees_edits(tmp_path):
    f = _custom(tmp_path)
    first = userfiles.folder_hash(f.folder)
    (f.folder / "hello_status" / "__pycache__").mkdir()
    (f.folder / "hello_status" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (f.folder / ".DS_Store").write_bytes(b"\0")
    assert userfiles.folder_hash(f.folder) == first
    assert set(userfiles.tree_files(f.folder)) == {"orch-addon.json", "hello_status/__init__.py"}


def test_one_byte_change_makes_it_changed(state):
    f = _custom(state)
    assert userfiles.trust_state(f) == "untrusted"
    userfiles.record_install(f.name, source={"kind": "path", "path": "/src"}, version="0.1.0", requires_api="2", folder=f.folder)
    assert userfiles.trust_state(f) == "untrusted"
    digest = userfiles.record_trust(f.name, f.folder, f.manifest)
    assert userfiles.trust_state(f) == "trusted" and digest == userfiles.digest_for(f)
    init = f.folder / "hello_status" / "__init__.py"
    original = init.read_text()
    init.write_text(original + " ")
    assert userfiles.trust_state(f) == "changed"
    init.write_text(original)
    assert userfiles.trust_state(f) == "trusted"
    entry = userfiles.registry_entries()[f.name]
    assert entry["trusted_permissions"]["binaries"] == ["git"] and "hello_status/__init__.py" in entry["trusted_files"]


def test_default_addons_are_trusted_by_plugin_version(tmp_path):
    import orch
    f = _custom(tmp_path)
    default = Found(f.name, "default", f.folder, f.manifest)
    assert userfiles.trust_state(default) == "trusted"
    assert userfiles.digest_for(default) == f"plugin-{orch.__version__}"


def test_invalid_found_is_invalid(tmp_path):
    assert userfiles.trust_state(Found("x", "custom", tmp_path, None, "broken")) == "invalid"


def test_forget_and_drop_everywhere(state, tmp_path):
    f = _custom(state)
    userfiles.record_install(f.name, source={"kind": "path", "path": "/src"}, version="0.1.0", requires_api="2", folder=f.folder)
    for name in ("ws1", "ws2"):
        (tmp_path / name).mkdir()
        userfiles.set_enabled(tmp_path / name, f.name, True)
    userfiles.forget(f.name)
    userfiles.drop_addon_everywhere(f.name)
    assert f.name not in userfiles.registry_entries()
    assert userfiles.workspace_addons(tmp_path / "ws1") == {} and userfiles.workspace_addons(tmp_path / "ws2") == {}


def test_tree_walk_is_capped_by_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(userfiles, "MAX_TREE_FILES", 2)
    folder = tmp_path / "big"
    folder.mkdir()
    for i in range(3):
        (folder / f"f{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="too large"):
        userfiles.tree_files(folder)
    with pytest.raises(ValidationError, match="too large"):
        userfiles.folder_hash(folder)


def test_tree_walk_is_capped_by_total_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(userfiles, "MAX_TREE_BYTES", 5)
    folder = tmp_path / "heavy"
    folder.mkdir()
    (folder / "f.txt").write_bytes(b"0123456789")
    with pytest.raises(ValidationError, match="too large"):
        userfiles.tree_files(folder)


def test_exceeding_a_cap_makes_trust_state_error(state, monkeypatch):
    f = _custom(state)
    userfiles.record_install(f.name, source={"kind": "path", "path": "/src"}, version="0.1.0", requires_api="2", folder=f.folder)
    userfiles.record_trust(f.name, f.folder, f.manifest)
    monkeypatch.setattr(userfiles, "MAX_TREE_FILES", 1)
    assert userfiles.trust_state(f) == "error"
    assert "too large" in userfiles.trust_problem(f)


def test_symlink_in_the_folder_is_not_allowed(state):
    f = _custom(state)
    userfiles.record_install(f.name, source={"kind": "path", "path": "/src"}, version="0.1.0", requires_api="2", folder=f.folder)
    userfiles.record_trust(f.name, f.folder, f.manifest)
    outside = state / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    link = f.folder / "hello_status" / "linked.txt"
    link.symlink_to(outside)
    with pytest.raises(ValidationError, match="symlinks are not allowed"):
        userfiles.tree_files(f.folder)
    with pytest.raises(ValidationError, match="symlinks are not allowed"):
        userfiles.folder_hash(f.folder)
    assert userfiles.trust_state(f) == "error"
    assert "symlinks are not allowed" in userfiles.trust_problem(f)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_file_gives_the_error_state(state):
    f = _custom(state)
    userfiles.record_install(f.name, source={"kind": "path", "path": "/src"}, version="0.1.0", requires_api="2", folder=f.folder)
    userfiles.record_trust(f.name, f.folder, f.manifest)
    secret = f.folder / "secret.txt"
    secret.write_text("shh", encoding="utf-8")
    secret.chmod(0)
    try:
        assert userfiles.trust_state(f) == "error"
        assert userfiles.trust_problem(f)
    finally:
        secret.chmod(0o644)


def test_read_json_object_never_blocks_on_a_fifo_and_caps_the_size(tmp_path):
    import os
    import threading
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    result = {}
    t = threading.Thread(target=lambda: result.update(v=userfiles.read_json_object(fifo)), daemon=True)
    t.start()
    t.join(2)
    if t.is_alive():  # unblock the reader so the test process can exit, then fail
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(2)
        pytest.fail("read_json_object blocked on a FIFO")
    assert result["v"] == {}
    big = tmp_path / "big.json"
    big.write_text('{"a": "' + "x" * (1024 * 1024) + '"}', encoding="utf-8")
    assert userfiles.read_json_object(big) == {}
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}', encoding="utf-8")
    assert userfiles.read_json_object(ok) == {"a": 1}
