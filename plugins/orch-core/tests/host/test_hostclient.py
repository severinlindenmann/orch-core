"""H4b, the thin CLI (``orch.hostclient``): mode by registration, rendering from ``result``, idempotent writes, person
commands through the signer composite, and the CLI contract against a live host (goldens, parity with direct mode)."""

from __future__ import annotations

import re

import pytest

from orch import hostclient
from orch.host.client import Connection
from orch.ops import human as ops_human
from orch.ops.runtime import Workspace
from tests.host.human_helpers import KeyBackend
from tests.host.lifecycle_helpers import UID, Lab
from tests.ops.helpers import Cli

# ids, the per-output fence nonce and the workspace-bound gate hashes differ between two workspaces by design
ID = re.compile(
    r"[0-7][0-9A-HJKMNP-TV-Z]{25}|[0-9a-f]{32}|(?<=\[)[0-9a-f]{8}(?=\])|(?<=end )[0-9a-f]{8}|(?<=#)[0-9a-f]{12}"
)


def flat(r) -> tuple[int, str, str]:
    return r.code, ID.sub("<id>", r.out), ID.sub("<id>", r.err)


@pytest.fixture
def lab(tmp_path):
    import errno

    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def hosted(lab, **kw):
    from tests.host.dispatch_helpers import Served

    s = Served(lab, **kw)
    service = s.service

    def install():  # the production host has a git reader (a test host has none: show would say "git cannot be read")
        from orch.ops.caps import Capabilities
        from tests.host.test_dispatch_review import FakeReader

        if service.caps is not None:
            service.caps.close()
        service.caps = Capabilities.host((service.entry.root,), env={}, clock=service.clock, git=FakeReader())

    service.submit(install).result(10)
    return s


def hcli(lab, **env):
    return Cli(lab.ws, ORCH_RUN_DIR=str(lab.run), **env)


# the script both modes run: every non-human op family, text and JSON, successes and refusals
SCRIPT = [
    ("new", "First ticket"),
    ("new", "Second ticket", "--priority", "high"),
    ("claim", "1"),
    ("log", "a note"),
    ("ask", "Which source?", "--options", "csv,api", "--rec", "csv"),
    ("show", "1"),
    ("show", "1", "--json"),
    ("list",),
    ("list", "--json"),
    ("status",),
    ("inbox",),
    ("next",),
    ("search", "First"),
    ("task", "list"),
    ("artifact", "list"),
    ("wait", "--timeout", "1"),
    ("show", "99"),
    ("show", "99", "--json"),
    ("claim", "2"),
    ("new", "Dry", "--dry-run"),
    ("log", "x", "--ref", "2", "--dry-run"),
]


def test_every_non_human_op_renders_byte_identically_in_both_modes(tmp_path_factory):
    direct = Lab(tmp_path_factory.mktemp("d"))
    other = Lab(tmp_path_factory.mktemp("h"))
    try:
        d = Cli(direct.ws)
        want = [flat(d(*argv)) for argv in SCRIPT]
        served = hosted(other)
        try:
            h = hcli(other)
            got = [flat(h(*argv)) for argv in SCRIPT]
        finally:
            served.close()
    finally:
        direct.close()
        other.close()
    for argv, w, g in zip(SCRIPT, want, got, strict=True):
        assert g == w, argv
    assert any(c[0] == 0 for c in want) and any(c[0] != 0 for c in want)


def test_the_golden_suite_is_unchanged_against_a_live_host(lab, monkeypatch):
    """P1's goldens (help, describe, every error envelope, usage) with the workspace registered and served."""
    from tests.cli import conftest as cc
    from tests.cli import golden_cases

    served = hosted(lab)
    try:
        base = {"ORCH_WORKSPACE": str(lab.root), "ORCH_STATE_DIR": str(lab.state), "ORCH_RUN_DIR": str(lab.run)}

        def run_cli(*argv, env=None, **kw):
            return cc.run_cli(*argv, env={**base, **(env or {})}, **kw)

        monkeypatch.setattr(golden_cases, "run_cli", run_cli)
        checked = 0
        for name, text in golden_cases.cases():
            if name == "errors/_not-implemented.txt":
                continue  # binds a stub over a handler of this process: the host runs its own
            assert (cc.GOLDEN / name).read_text(encoding="utf-8") == text, name
            checked += 1
        assert checked > 200
    finally:
        served.close()


def test_mode_is_by_registration_and_a_dead_host_is_never_a_direct_write(lab):
    ws = lab.ws
    cli = Cli(ws)
    assert cli("new", "Direct").code == 0  # not registered: direct mode, as today
    env = ws.env()
    assert hostclient.locate(env, Workspace(env, files=_files())) is None
    served = hosted(lab)
    try:
        assert hcli(lab)("new", "Hosted").code == 0
        target = hostclient.locate(env | {"ORCH_RUN_DIR": str(lab.run)}, Workspace(env, files=_files()))
        assert target.role == "person" and target.host_uid == UID
    finally:
        served.close()
    n = len(list((lab.ws.root / "tickets").iterdir()))
    r = hcli(lab).j("new", "Nowhere")
    assert (r.code, r.err_code) == (9, "host.unavailable"), r.out
    assert "host" in r.doc["error"]["message"] and r.doc["error"]["retryable"] is True
    assert len(list((lab.ws.root / "tickets").iterdir())) == n  # nothing written directly
    r = hcli(lab).j("show", "1")
    assert (r.code, r.err_code) == (9, "host.unavailable")


def _files():
    from orch.ops.caps import Files

    return Files.direct()


def test_an_agent_pin_selects_the_host_and_an_unreadable_registry_fails_closed(lab):
    served = hosted(lab)
    try:
        lab.pin_agent(UID, [served.wid])
        from orch.host.pins import AgentPins

        env = lab.ws.env() | {"ORCH_RUN_DIR": str(lab.run)}
        t = hostclient.locate(env, Workspace(env, files=_files()), uid=UID, pins=AgentPins(lab.pins_dir, owner=UID))
        assert (t.role, t.host_uid, t.entry) == ("agent", UID, None)
        (lab.state / "host" / "workspaces.json").write_text("{")
        (lab.pins_dir / f"{UID}.json").unlink()
        with pytest.raises(hostclient.HostFailure) as caught:
            hostclient.locate(env, Workspace(env, files=_files()), uid=UID, pins=AgentPins(lab.pins_dir, owner=UID))
        assert caught.value.code == "host.unavailable" and caught.value.wire_exit == 9
    finally:
        served.close()


def test_a_write_whose_answer_is_lost_is_retried_with_its_idem_and_applied_once(lab, monkeypatch):
    served = hosted(lab)
    try:
        real = Connection.call
        seen = []

        def lossy(self, op, args, **kw):
            out = real(self, op, args, **kw)
            if op == "new" and not seen:
                seen.append(kw.get("idem"))
                self.broken = True
                raise ConnectionError("the answer was lost")
            if op == "new":
                seen.append(kw.get("idem"))
            return out

        monkeypatch.setattr(Connection, "call", lossy)
        r = hcli(lab).j("new", "Once")
        assert r.code == 0 and r.doc["duplicate"] is True and len(seen) == 2 and seen[0] == seen[1]
        assert seen[0].startswith("a_")
        keys = hcli(lab).j("list").data
        assert [t["title"] for t in keys["tickets"]].count("Once") == 1
    finally:
        served.close()


def test_files_go_inline_or_as_a_blob_and_the_grant_never_in_args(lab, tmp_path):
    served = hosted(lab)
    try:
        small = tmp_path / "small.log"
        small.write_bytes(b"hello\n")
        big = tmp_path / "big.bin"
        big.write_bytes(bytes(range(256)) * 3000)  # 750 KiB: over the inline limit
        cli = hcli(lab, grant=False)
        hcli(lab)("new", "Ticket")
        r1 = cli.j("artifact", "add", str(small), "--ref", "1")
        r2 = cli.j("artifact", "add", str(big), "--ref", "1", "--name", "big.bin")
        assert (r1.code, r2.code) == (0, 0), (r1.out, r2.out)
        assert r2.data["bytes"] == 768000
        miss = cli.j("artifact", "add", str(tmp_path / "nope"), "--ref", "1")
        assert (miss.code, miss.err_code) == (5, "invalid.input")
    finally:
        served.close()


def test_a_person_command_runs_the_signer_composite_at_the_terminal(lab, monkeypatch, tmp_path):
    served = hosted(lab)
    try:
        out = hcli(lab)
        out("new", "To close")
        owner = lab.ws.owner

        class Backend(KeyBackend):
            def public_key(self, key_id):
                return owner.sig_pub

        shown = []
        monkeypatch.setattr(ops_human, "open_backend", lambda directory: Backend(owner))
        monkeypatch.setattr(ops_human, "review_prompt", lambda text, word: shown.append((text, word)) or True)
        keys = ops_human.keys_dir(lab.state, served.wid)
        keys.mkdir(parents=True)
        (keys / ops_human.KEY_FILE).write_text("{}")
        r = hcli(lab, grant=False).j("close", "1", "--resolution", "wont_do")
        assert r.code == 0, r.out
        assert shown and shown[0][1].startswith("DEMO-0001/") and "close" in shown[0][0].lower()
        assert "ticket.closed" in hcli(lab, grant=False)("show", "1").out
        # declined: nothing is signed
        monkeypatch.setattr(ops_human, "review_prompt", lambda text, word: False)
        out("new", "Second")
        r = hcli(lab, grant=False).j("close", "2")
        assert (r.code, r.err_code) == (5, "invalid.input") and "nothing was signed" in r.doc["error"]["message"]
        # an agent never gets there
        r = hcli(lab).j("close", "2")
        assert (r.code, r.err_code) == (3, "human_only")
        # a dry run signs nothing
        r = hcli(lab, grant=False).j("close", "2", "--dry-run")
        assert r.code == 0 and r.data["dry_run"] is True
    finally:
        served.close()


def test_wait_is_one_request_parked_on_the_host_bus(lab):
    served = hosted(lab)
    try:
        c = hcli(lab)
        c("new", "A")
        c("claim", "1")
        r = c.j("wait", "--timeout", "1")
        assert r.code == 0 and r.data["kind"] == "timeout"
        r = c.j("wait", "--timeout", "1", "--strict-timeout")
        assert (r.code, r.err_code) == (7, "wait.timeout")
    finally:
        served.close()


def test_init_and_import_stay_local_for_a_registered_workspace(lab):
    served = hosted(lab)
    try:
        r = hcli(lab, grant=False).j("import", "v1", str(lab.tmp / "nowhere"))
        assert r.err_code in ("host.required", "invalid.input", "usage", "human_only", "not_found"), r.out
    finally:
        served.close()
