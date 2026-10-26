"""H4a1 units: the F6 visibility hooks, the authentication snapshot, the request scan for grant secrets, the session
records keyed by principal, the result builder, and the H3a notes (no direct-mode guard in host mode, no discovery
for the host's workspace, ``audit.warm`` skipping test packages)."""

from __future__ import annotations

import base64
import sys
from types import SimpleNamespace

import pytest

from orch import crypto  # noqa: E402
from orch.cli.session import MemoryRecords
from orch.host.auth import AuthView, GrantEntry, secret_in_request
from orch.host.dispatch import ScopedRecords, Viewer, rebuild
from orch.host.errors import HostError

SECRET_BYTES = b"a" * 32
GRANT = "gr_01J9ZP0000000000000000000G." + crypto.b64u(SECRET_BYTES)


def view(key="DEMO-0001", visibility="workspace", repos=(), artifacts=()):
    return SimpleNamespace(
        key=key,
        visibility=visibility,
        fields={"links": {"repos": list(repos)}},
        artifacts=[SimpleNamespace(name=n) for n in artifacts],
    )


class Store:
    def __init__(self, *views):
        self.views = {v.key: v for v in views}

    def normalise_ref(self, ref):
        return ref

    def ticket(self, ref):
        return self.views.get(ref)


OPEN = view("DEMO-0001")
SECRET = view("DEMO-0002", {"restricted": ["p_lena"]}, repos=["proj"], artifacts=["report.txt"])
STORE = Store(OPEN, SECRET)


# --- F6 visibility hooks ------------------------------------------------------------------------------------------


def test_a_ref_the_viewer_may_not_see_is_not_found_exactly_like_one_that_does_not_exist():
    lena, nobody, agent = Viewer("P", "p_lena"), Viewer("N", None), Viewer("G", "p_max")
    assert lena.ticket(STORE, "DEMO-0002/T1") is SECRET
    for v in (nobody, agent):
        with pytest.raises(HostError) as hidden:
            v.ticket(STORE, "DEMO-0002")
        with pytest.raises(HostError) as missing:
            v.ticket(STORE, "DEMO-0099")
        assert hidden.value.code == missing.value.code == "not_found"
        assert hidden.value.message.replace("0002", "N") == missing.value.message.replace("0099", "N")
    assert Viewer("N", None).ticket(STORE, "DEMO-0001") is OPEN


def test_rows_are_filtered_before_they_are_counted():
    rows = [OPEN, SECRET, view("DEMO-0003")]
    assert [v.key for v in Viewer("N", None).visible(rows)] == ["DEMO-0001", "DEMO-0003"]
    assert len(Viewer("G", "p_lena").visible(rows)) == 3


def test_needs_person_from_an_agent_a_person_and_no_person():
    agent = Viewer("G", "p_max")
    assert agent.needs_person(None) == "p_max"
    assert agent.needs_person("p_max") == "p_max"
    with pytest.raises(HostError) as e:
        agent.needs_person("p_lena")  # an agent never reads another person's needs
    assert e.value.code == "op.not_allowed"
    assert Viewer("P", "p_max", owner=True).needs_person("p_lena") == "p_lena"
    assert Viewer("P", "p_max").needs_person(None) == "p_max"
    with pytest.raises(HostError):
        Viewer("P", "p_max").needs_person("p_lena")
    assert Viewer("N", None).needs_person(None) is None
    with pytest.raises(HostError):
        Viewer("N", None).needs_person("p_lena")


def test_session_entries_and_their_claims():
    assert Viewer("P", "p_max", owner=True).session_visible("p_lena")
    assert Viewer("P", "p_max").session_visible("p_max") and not Viewer("P", "p_max").session_visible("p_lena")
    assert Viewer("G", "p_max").session_visible("p_max") and not Viewer("G", "p_max").session_visible("p_lena")
    assert not Viewer("N", None).session_visible(None)
    assert Viewer("G", "p_max").claims(STORE, ["DEMO-0001", "DEMO-0002", "DEMO-0099"]) == ["DEMO-0001"]
    assert Viewer("P", "p_lena").claims(STORE, ["DEMO-0002"]) == ["DEMO-0002"]


def test_diff_on_an_unlinked_repo_and_artifact_get_with_a_path_are_not_found():
    Viewer.linked_repo(SECRET, "proj")
    Viewer.linked_repo(SECRET, None)
    for repo in ("other", "../proj", "proj/.."):
        with pytest.raises(HostError) as e:
            Viewer.linked_repo(SECRET, repo)
        assert e.value.code == "not_found"
    assert Viewer.artifact(SECRET, "report.txt").name == "report.txt"
    for name in ("../x", "/etc/passwd", "report.txt/..", "..", "", "a" * 200, "missing.txt"):
        with pytest.raises(HostError) as e:
            Viewer.artifact(SECRET, name)
        assert e.value.code == "not_found"


def test_sub_open_on_a_restricted_ticket_is_not_found():
    """The named acceptance item (H6a wires sub.open {ticket} through Viewer.ticket, as show and ticket.get)."""
    with pytest.raises(HostError) as e:
        Viewer("G", "p_max").ticket(STORE, "DEMO-0002")
    assert e.value.code == "not_found"


def test_viewer_of_a_call_is_an_owner_only_in_column_p():
    from orch.host.columns import Column

    auth = AuthView(None, {}, {"p_own": "owner", "p_mem": "member"}, {})
    for column, person, owner in [
        ("P", "p_own", True),
        ("P", "p_mem", False),
        ("G", "p_own", False),
        ("N", None, False),
    ]:
        call = SimpleNamespace(service=SimpleNamespace(auth=auth), column=Column(column, "x"), viewer=person)
        v = Viewer.of(call)
        assert (v.column, v.person, v.owner) == (column, person, owner)


# --- the authentication snapshot ----------------------------------------------------------------------------------


def _auth(expires_s=2_000, revoked=False, role="member"):
    from orch import canon

    entry = GrantEntry(
        "gr_01J9ZP0000000000000000000G",
        "p_max",
        canon.grant_secret_hash(SECRET_BYTES),
        revoked,
        "x",
        expires_s,
        "agent",
        "all",
    )
    return AuthView((1, 2, 3), {entry.id: entry}, {"p_max": role}, {"p_max": (None,)})


def test_a_grant_is_judged_at_request_time():
    auth = _auth()
    assert auth.verify(GRANT, 1_000).state == "verified"
    assert auth.verify(GRANT, 2_000).state == "unverified"  # expired since the snapshot was built
    assert _auth(revoked=True).verify(GRANT, 1_000).state == "unverified"
    assert _auth(role="viewer").verify(GRANT, 1_000).state == "unverified"
    other = "gr_01J9ZP0000000000000000000G." + crypto.b64u(b"b" * 32)
    assert auth.verify(other, 1_000).state == "unverified"  # the wrong secret
    assert auth.verify("gr_nope", 1_000).state == "malformed"
    assert auth.verify(None, 1_000).state == "absent"
    assert auth.person_ok("p_max", 1_000) and not auth.person_ok("p_lena", 1_000) and not auth.person_ok(None, 0)
    expiring = AuthView(None, {}, {"p_max": "member"}, {"p_max": (5_000,)})
    assert expiring.person_ok("p_max", 4) and not expiring.person_ok("p_max", 5)
    assert not expiring.fresh("/nonexistent")  # no stamp: never fresh, the worker thread decides


def test_the_request_scan_finds_a_grant_or_the_presented_secret_anywhere():
    assert secret_in_request({"args": {"a": [{"b": GRANT}]}})
    assert secret_in_request({"args": {GRANT: 1}})
    assert secret_in_request({"args": {}, "opts": {"idem": GRANT}})
    assert secret_in_request({"args": {"t": "x" + GRANT.partition(".")[2]}, "auth": {"grant": GRANT}})
    files = {"f": {"b64": base64.b64encode(b"..." + GRANT.encode()).decode()}}
    assert secret_in_request({"args": {}, "files": files})
    assert not secret_in_request({"args": {"t": "hello"}, "auth": {"grant": GRANT}})
    assert not secret_in_request({"args": {}, "files": {"f": {"b64": "!!not base64"}}})


# --- records, builder -------------------------------------------------------------------------------------------------


def test_session_records_are_keyed_by_principal_and_session():
    inner = MemoryRecords()
    a, b = ScopedRecords(inner, "g:gr_X@501"), ScopedRecords(inner, "uid:501")
    for _ in range(3):
        a.refused("s_1", "log", {"x": 1}, "not_found", 100.0)
    assert a.stopped("s_1", "log", {"x": 1}, 101.0)
    assert not b.stopped("s_1", "log", {"x": 1}, 101.0)  # the same label from another principal
    a.remember("s_1", "k", 100.0, {"data": 1})
    assert a.recall("s_1", "k", 101.0) == {"data": 1} and b.recall("s_1", "k", 101.0) is None
    assert inner.recall("s_1", "k", 101.0) is None  # nor the bare session (direct mode)


def test_the_result_builder_rebuilds_key_seq_and_new_data():
    created = {"type": "ticket.created", "seq": 1, "key": "DEMO-0007", "title": "t", "ticket_type": "bug"}
    updated = {"type": "ticket.updated", "seq": 2}
    out = rebuild({"op": "new"}, [created, updated])
    assert out["key"] == "DEMO-0007" and out["seq"] == 2 and out["data"] == {"title": "t", "ticket_type": "bug"}
    out = rebuild({"op": "log", "key": "DEMO-0003"}, [{"type": "log.added", "seq": 9}])
    assert out["key"] == "DEMO-0003" and out["seq"] == 9 and out["data"] == {}


# --- the H3a notes ----------------------------------------------------------------------------------------------------


def test_a_host_mode_workspace_never_takes_the_direct_mode_guard(tmp_path, monkeypatch):
    """(a) the guard comes from the caps: host-mode files open the store without ``DirectGuard``."""
    import orch.host.ownership as ownership
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace
    from tests.ops.helpers import Ws

    ws = Ws(tmp_path)
    ws.bootstrap()
    ws.store.close()

    def refuse(*a, **k):
        raise AssertionError("DirectGuard in host mode")

    monkeypatch.setattr(ownership, "DirectGuard", refuse)
    env = {"ORCH_WORKSPACE": str(ws.root), "ORCH_STATE_DIR": str(ws.host_state)}
    with Files.host((ws.root, ws.host_state)) as files:
        store = Workspace(env, lambda: ws.clock[0], files=files).store
        store.close()
    with pytest.raises(AssertionError):
        Workspace(env, lambda: ws.clock[0], files=Files.direct()).store  # noqa: B018 - direct mode still guards


def test_the_hosted_workspace_discovers_nothing(tmp_path, monkeypatch):
    """(b) root discovery, ``config.json`` and the store are the host's own: nothing is looked up."""
    import orch.ops.runtime as runtime
    from orch.ops.caps import Files

    def refuse(*a, **k):
        raise AssertionError("discovery in host mode")

    monkeypatch.setattr(runtime, "find_workspace", refuse)
    monkeypatch.setattr(runtime, "_config", refuse)
    sentinel = object()
    with Files.host((tmp_path,)) as files:
        ws = runtime.Workspace.hosted(tmp_path, sentinel, env={}, now=lambda: 0.0, files=files)
        assert ws.root == tmp_path and ws.store is sentinel and ws.require_root() == tmp_path


def test_warm_skips_test_and_benchmark_packages(tmp_path, monkeypatch):
    """(c) a third-party tree's ``tests``/``benchmarks`` packages are never imported (nor recursed into)."""
    from orch.host import audit

    pkg = tmp_path / "thirdp"
    for sub in ("", "core", "tests", "benchmarks", "core/tests"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "tests" / "__init__.py").write_text("raise RuntimeError('imported a test package')")
    (pkg / "core" / "tests" / "__init__.py").write_text("raise RuntimeError('imported a nested test package')")
    (pkg / "benchmarks" / "__init__.py").write_text("raise RuntimeError('imported benchmarks')")
    (pkg / "core" / "mod.py").write_text("X = 1")
    monkeypatch.syspath_prepend(str(tmp_path))
    audit._import_tree("thirdp")
    assert "thirdp.core.mod" in sys.modules
    assert not any(name.startswith(("thirdp.tests", "thirdp.benchmarks", "thirdp.core.tests")) for name in sys.modules)
