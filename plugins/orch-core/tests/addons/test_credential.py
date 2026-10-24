"""V3 G6 (#924) C2/C4: the one reader and writer of an add-on's key (``orch.addons.credential``)."""

from __future__ import annotations

import copy
import json
import os
import pickle
from types import SimpleNamespace

import pytest

from orch.addons import credential as cr
from orch.addons import install
from orch.host import marketinstall
from orch.host.errors import HostError

UID = os.getuid()
VALUE = "k-synthetic-0123456789"


@pytest.fixture
def state(tmp_path):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    return tmp_path


def put(state, name="demo", value=VALUE, now=100.0):
    cr.store(state, "ws", UID, name, cr.Secret(value), now)


def file_of(state, name="demo"):
    return state / "hosts" / "ws" / "addons" / name / "credential"


def test_roundtrip_modes_and_content(state):
    put(state)
    f = file_of(state)
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(f.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(f.parent.parent.stat().st_mode & 0o777) == "0o700"
    assert json.loads(f.read_text()) == {"v": 1, "addon": "demo", "value": VALUE, "stored_at": 100}
    assert cr.load(state, "ws", UID, "demo").reveal() == VALUE
    assert cr.status(state, "ws", UID, "demo") == "stored" and cr.status(state, "ws", UID, "other") == "missing"
    put(state, value="second-value")  # replace
    assert cr.load(state, "ws", UID, "demo").reveal() == "second-value"
    assert [p.name for p in f.parent.iterdir()] == ["credential"]  # no temp left


def test_secret_is_opaque():
    s = cr.Secret(VALUE)
    for text in (repr(s), str(s), f"{s}", f"{s!r}"):
        assert VALUE not in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(s)
    with pytest.raises(TypeError):
        hash(s)


@pytest.mark.parametrize("bad", ["", "a b", "tab\t", "é", "x" * 1025, "nl\n", 5, None])
def test_shape_refused(bad):
    assert not cr.valid(bad)
    with pytest.raises(ValueError):
        cr.Secret(bad)
    assert cr.valid("x" * 1024)


@pytest.mark.parametrize("name", ["", "Demo", "../x", "a/b", "-a", "a" * 41, "a_b", ".hidden"])
def test_names_refused(state, name):
    with pytest.raises(ValueError):
        cr.addon_dir(state, "ws", name)
    with pytest.raises(ValueError):
        put(state, name)


def test_a_file_stored_for_another_addon_is_skipped(state):
    put(state, "demo")
    other = state / "hosts" / "ws" / "addons" / "other"
    other.mkdir(mode=0o700)
    (other / "credential").write_bytes(file_of(state).read_bytes())  # copied: addon says "demo"
    os.chmod(other / "credential", 0o600)
    assert cr.load(state, "ws", UID, "other") is None
    assert cr.provider(state, "ws", UID, "other", ["credential", "network"])() is None


@pytest.mark.parametrize("problem", ["file_mode", "dir_mode", "file_link", "dir_link", "hardlink", "owner", "junk"])
def test_custody_refusals(state, tmp_path, problem):
    put(state)
    f = file_of(state)
    host_uid = UID
    if problem == "file_mode":
        f.chmod(0o644)
    elif problem == "dir_mode":
        f.parent.chmod(0o755)
    elif problem == "file_link":
        real = tmp_path / "real"
        real.write_bytes(f.read_bytes())
        f.unlink()
        f.symlink_to(real)
    elif problem == "dir_link":
        moved = f.parent.with_name("moved")
        f.parent.rename(moved)
        f.parent.symlink_to(moved)
    elif problem == "hardlink":
        os.link(f, tmp_path / "second")
    elif problem == "owner":
        host_uid = UID + 1
    elif problem == "junk":
        f.write_text("not json")
    with pytest.raises(HostError) as caught:
        cr.load(state, "ws", host_uid, "demo")
    assert caught.value.code == "custody.unsafe_mode"
    assert VALUE not in str(caught.value)


def test_provider_c2_needs_credential_and_network_in_the_active_grant(state):
    put(state)
    ask = lambda caps, **kw: cr.provider(state, "ws", UID, "demo", caps, **kw)()  # noqa: E731
    assert ask(["credential", "network"]) == VALUE
    assert ask(["credential"]) is None
    assert ask(["network"]) is None
    assert ask([]) is None
    assert ask(["credential", "network"], active=False) is None
    # a grant that does not qualify never opens the file: a compromised file does not even raise
    file_of(state).chmod(0o644)
    assert ask(["credential"]) is None and ask(["credential", "network"], active=False) is None
    with pytest.raises(HostError):
        ask(["credential", "network"])


def test_remove_is_idempotent_and_exact(state):
    put(state)
    put(state, "keep")
    assert cr.remove(state, "ws", "demo") is True
    assert not file_of(state).parent.exists()
    assert cr.remove(state, "ws", "demo") is False  # idempotent
    assert cr.remove(state, "ws", "never") is False
    assert cr.status(state, "ws", UID, "keep") == "stored"


def test_remove_of_a_swapped_link_does_not_follow_it(state, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "credential").write_text("precious")
    base = state / "hosts" / "ws" / "addons"
    base.mkdir(mode=0o700)
    (base / "demo").symlink_to(victim)
    cr.remove(state, "ws", "demo")
    assert (victim / "credential").read_text() == "precious" and not (base / "demo").exists()


def test_sweep_keeps_only_listed_names_and_is_idempotent(state):
    for n in ("a", "b", "c"):
        put(state, n)
    (state / "hosts" / "ws" / "addons" / ".Weird").mkdir()  # not an add-on name: left alone
    assert cr.sweep(state, "ws", ["b"]) == ["a", "c"]
    assert cr.sweep(state, "ws", ["b"]) == []
    assert cr.status(state, "ws", UID, "b") == "stored"
    assert cr.sweep(state, "other-ws", []) == []  # no directory: nothing


def _service(state, granted):
    return SimpleNamespace(
        state_dir=state,
        workspace_id="ws",
        store=SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(addons=None))),
    ), granted


def test_c4_purge_uninstall_capability_drop_delete_the_key(state, monkeypatch):
    for n in ("live", "purged", "dropped", "ungranted"):
        put(state, n)
    svc, granted = _service(state, None)
    granted = {
        "live": SimpleNamespace(purged=False, capabilities=["credential", "network"]),
        "purged": SimpleNamespace(purged=True, capabilities=["credential", "network"]),
        "dropped": SimpleNamespace(purged=False, capabilities=["network"]),  # a grant that dropped the capability
    }
    monkeypatch.setattr(install, "as_addons", lambda view: granted)
    marketinstall.sweep_credentials(svc)
    assert {n: cr.status(state, "ws", UID, n) for n in ("live", "purged", "dropped", "ungranted")} == {
        "live": "stored",
        "purged": "missing",
        "dropped": "missing",
        "ungranted": "missing",
    }
    marketinstall.sweep_credentials(svc)  # idempotent at start
    assert cr.status(state, "ws", UID, "live") == "stored"


# --- A2-1: the module never leaves the checked ``addons`` fd ------------------------------------------------------


def _linked_addons(state, tmp_path):
    victim = tmp_path / "victim"
    (victim / "projects").mkdir(parents=True)
    os.chmod(victim, 0o700)
    os.chmod(victim / "projects", 0o755)
    (victim / "projects" / "notes.txt").write_text("precious")
    os.symlink(victim, state / "hosts" / "ws" / "addons")
    return victim


def test_store_through_a_symlinked_addons_dir_is_refused_and_writes_nothing(state, tmp_path):
    victim = _linked_addons(state, tmp_path)
    with pytest.raises(HostError) as caught:
        put(state, "probe")
    assert caught.value.code == "custody.unsafe_mode"
    assert not (victim / "probe").exists()


def test_load_through_a_symlinked_addons_dir_is_refused(state, tmp_path):
    victim = _linked_addons(state, tmp_path)
    (victim / "probe").mkdir(mode=0o700)
    (victim / "probe" / "credential").write_text(json.dumps({"v": 1, "addon": "probe", "value": VALUE, "stored_at": 1}))
    (victim / "probe" / "credential").chmod(0o600)
    with pytest.raises(HostError):
        cr.load(state, "ws", UID, "probe")


def test_sweep_and_remove_through_a_symlinked_addons_dir_delete_nothing_elsewhere(state, tmp_path):
    victim = _linked_addons(state, tmp_path)
    with pytest.raises(HostError):
        cr.sweep(state, "ws", keep=[])
    with pytest.raises(HostError):
        cr.remove(state, "ws", "projects")
    assert (victim / "projects" / "notes.txt").read_text() == "precious"


def test_a_loose_entry_is_left_alone_and_refused_never_emptied(state):
    put(state, "good")
    put(state, "stray")
    base = state / "hosts" / "ws" / "addons"
    os.chmod(base / "stray", 0o755)
    (base / "stray" / "keepme").write_text("x")
    with pytest.raises(HostError) as caught:
        cr.sweep(state, "ws", keep=[])
    assert caught.value.code == "custody.unsafe_mode"
    assert (base / "stray" / "keepme").read_text() == "x" and (base / "stray" / "credential").exists()
    assert not (base / "good").exists()  # the rest was still swept
    with pytest.raises(HostError):
        cr.remove(state, "ws", "stray")


def test_a_link_or_file_planted_as_the_entry_is_unlinked_not_followed(state, tmp_path):
    put(state, "keep")
    base = state / "hosts" / "ws" / "addons"
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    (base / "viafile").write_text("junk")
    (base / "vialink").symlink_to(outside)
    assert cr.sweep(state, "ws", keep=["keep"]) == ["viafile", "vialink"]
    assert outside.read_text() == "precious" and sorted(p.name for p in base.iterdir()) == ["keep"]
