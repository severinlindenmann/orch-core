"""H4b, the thin CLI (``orch.hostclient``): mode by registration, rendering from ``result``, idempotent writes, person
commands through the signer composite, and the CLI contract against a live host (goldens, parity with direct mode)."""

from __future__ import annotations

import json
import re

import pytest

from orch import hostclient
from orch.host.client import Connection
from orch.ops import human as ops_human
from orch.ops.runtime import Workspace
from tests.host.human_helpers import KeyBackend
from tests.host.lifecycle_helpers import UID, Lab
from tests.ops.helpers import Cli
from tests.store.helpers import WS

# What differs between two workspaces by design: the workspace id, the per-output fence nonce and the gate hashes that
# bind it and each lab's owner key and grant. Nothing else is masked: ticket uids, digests and other hashes must match.
NONCE = re.compile(r"(?<=\[)[0-9a-f]{8}(?=\])|(?<=--- end )[0-9a-f]{8}")
GATE = re.compile(r"(?<=#)[0-9a-f]{12}")
PERSON = re.compile(r"p_[0-9a-f]{32}|gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")  # each lab draws its own owner key and grant


def flat(r, wid: str, root=None) -> tuple[int, str, str]:
    def mask(text: str) -> str:
        if root is not None:  # the project folder differs per lab (a work folder line names its full path)
            text = text.replace(str(root), "<root>")
        return PERSON.sub("<person>", GATE.sub("<gate>", NONCE.sub("<nonce>", text.replace(wid, "<ws>"))))

    return r.code, mask(r.out), mask(r.err)


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
    return Cli(lab.ws, ORCH_HOST_RUN_DIR=str(lab.run), **env)


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
        want = [flat(d(*argv), WS, direct.root) for argv in SCRIPT]
        served = hosted(other)
        try:
            h = hcli(other)
            got = [flat(h(*argv), WS, other.root) for argv in SCRIPT]
        finally:
            served.close()
    finally:
        direct.close()
        other.close()
    for argv, w, g in zip(SCRIPT, want, got, strict=True):
        assert g == w, argv
    assert any(c[0] == 0 for c in want) and any(c[0] != 0 for c in want)


ADDON_OPS = {"quick.list", "follow_up", "follow_up.list", "follow_up.show", "follow_up.cancel", "webhook.new"}
WORKSPACE_DEPENDENT = {
    "describe/_all.txt", "describe/_all.json", "help/_all.txt", "help/_all.json",
    "help/workflow.wait.txt", "help/workflow.wait.json",
}  # fmt: skip


def test_the_local_golden_paths_are_unchanged_with_the_workspace_registered(lab, monkeypatch):
    """The golden cases are help, describe, rendered error envelopes and parser errors: none of them is a request to
    the host. This only shows that those stay local and unchanged while the workspace is registered and served; the
    parity of everything that does reach the host is ``test_every_non_human_op_renders_byte_identically...``."""
    from tests.cli import conftest as cc
    from tests.cli import golden_cases

    served = hosted(lab)
    try:
        base = {"ORCH_WORKSPACE": str(lab.root), "ORCH_STATE_DIR": str(lab.state), "ORCH_HOST_RUN_DIR": str(lab.run)}

        def run_cli(*argv, env=None, **kw):
            return cc.run_cli(*argv, env={**base, **(env or {})}, **kw)

        monkeypatch.setattr(golden_cases, "run_cli", run_cli)
        checked = 0
        for name, text in golden_cases.cases():
            if name == "errors/_not-implemented.txt":
                continue  # binds a stub over a handler of this process: the host runs its own
            if name in WORKSPACE_DEPENDENT or name.split("/")[-1].rsplit(".", 1)[0] in ADDON_OPS:
                continue  # help and describe list an add-on's commands only while it is on: this workspace has none on
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
        target = hostclient.locate(env | {"ORCH_HOST_RUN_DIR": str(lab.run)}, Workspace(env, files=_files()))
        assert target.role == "person" and target.host_uid == UID
    finally:
        served.close()
    n = len(list((lab.ws.store_dir / "tickets").iterdir()))
    r = hcli(lab).j("new", "Nowhere")
    assert (r.code, r.err_code) == (9, "host.unavailable"), r.out
    assert "host" in r.doc["error"]["message"] and r.doc["error"]["retryable"] is True
    assert len(list((lab.ws.store_dir / "tickets").iterdir())) == n  # nothing written directly
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

        env = lab.ws.env() | {"ORCH_HOST_RUN_DIR": str(lab.run)}
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


def test_an_addon_grant_finds_the_device_key_where_init_put_it(lab, monkeypatch, tmp_path):
    from orch.host.marketstate import MarketFiles
    from orch.market import constants
    from tests.host.lifecycle_helpers import UID
    from tests.host.market_helpers import Net, seed_market, write_stage
    from tests.market import helpers as H

    served = hosted(lab)
    try:
        owner = lab.ws.owner

        class Backend(KeyBackend):
            def public_key(self, key_id):
                return owner.sig_pub

        monkeypatch.setattr(ops_human, "open_backend", lambda directory: Backend(owner))
        monkeypatch.setattr(ops_human, "review_prompt", lambda text, word: True)
        keys = ops_human.keys_dir(lab.state, served.wid)
        keys.mkdir(parents=True)
        (keys / ops_human.KEY_FILE).write_text("{}")
        monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64,))
        files = MarketFiles(lab.state, served.wid, UID)
        stage = write_stage(files, "lint")
        n = Net()
        n.add("lint", "1.0.0")
        seed_market(files, n.entries)  # what `orch addon install` leaves
        r = hcli(lab, grant=False).j("addon", "grant", "lint", "--staged", stage)
        assert r.code == 0, r.out
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
        # a dry run signs nothing, and answers what direct mode's --dry-run answers (H7: the host sends the result)
        before = lab.ws.read_events(lab.ws.uid("2"))
        r = hcli(lab, grant=False).j("close", "2", "--dry-run")
        assert r.code == 0 and r.doc["key"] == "DEMO-0002" and "dry_run" not in (r.data or {})
        assert hcli(lab, grant=False)("close", "2", "--dry-run").out.splitlines()[1] == "dry-run: nothing was written"
        assert lab.ws.read_events(lab.ws.uid("2")) == before
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


def test_import_stays_local_and_the_person_registry_guard_answers_host_required(lab):
    served = hosted(lab)
    try:
        r = hcli(lab, grant=False).j("import", "v1", str(lab.tmp / "nowhere"))
        # it ran locally (the importer looked for the v1 folder): no request reached the host, no host.unavailable
        assert (r.code, r.err_code) == (2, "not_found"), r.out
        # the person-registry path of the direct guard (not only an agent pin)
        from orch.host.ownership import DirectGuard

        with pytest.raises(Exception) as caught:
            DirectGuard(lab.state, served.wid).check()
        assert getattr(caught.value, "code", None) == "host.required"
    finally:
        served.close()


def _done_events(lab, key):
    return [e for e in lab.ws.events(key) if e["type"] == "task.done"]


def test_task_done_run_runs_the_command_here_and_sends_a_labelled_receipt(lab):
    served = hosted(lab)
    try:
        c = hcli(lab)
        c("new", "A")
        c("claim", "1")
        c("task", "add", "do it", "--verify", "echo hello-from-verify")
        c("task", "start", "T1")
        r = c.j("task", "done", "T1", "--run")
        assert r.code == 0, r.out
        (done,) = _done_events(lab, "1")
        assert done["receipt"]["cmd"] == "echo hello-from-verify" and done["receipt"]["exit"] == 0
        assert done["receipt"]["attested_by"] == "agent" and done["receipt"]["repo"] is None
        events = lab.ws.events("1")
        art = next(e for e in events if e["type"] == "artifact.added" and e["kind"] == "receipt")
        assert art["name"] == done["log"]
        shown = c.j("task", "list").doc
        assert shown["data"]["tasks"][0]["verify"] == "echo hello-from-verify"
    finally:
        served.close()


def test_task_done_run_text_says_who_attests_and_a_failing_command_sends_nothing(lab):
    served = hosted(lab)
    try:
        c = hcli(lab)
        c("new", "A")
        c("claim", "1")
        c("task", "add", "fails", "--verify", "false")
        c("task", "start", "T1")
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (5, "verify.failed"), r.out
        assert _done_events(lab, "1") == []
        c("task", "add", "passes", "--verify", "true")
        c("task", "start", "T2")
        text = c("task", "done", "T2", "--run")
        assert text.code == 0 and "attested by you" in text.out, text.out
    finally:
        served.close()


def test_task_done_never_forwards_run_and_dry_run_runs_nothing(lab):
    served = hosted(lab)
    try:
        c = hcli(lab)
        c("new", "A")
        c("claim", "1")
        c("task", "add", "x", "--verify", "true")
        c("task", "start", "T1")
        r = c("task", "done", "T1", "--run", "--dry-run")
        assert r.code == 0 and "would run" in r.out, r.out
        assert _done_events(lab, "1") == []
        sent = []
        orig = hostclient._request

        def spy(target, op, args, **kw):
            sent.append((op if not kw.get("dry_run") else op + "?dry", dict(args)))
            return orig(target, op, args, **kw)

        import pytest as _pt

        mp = _pt.MonkeyPatch()
        mp.setattr(hostclient, "_request", spy)
        try:
            assert c("task", "done", "T1", "--run").code == 0
        finally:
            mp.undo()
        done_args = next(a for o, a in sent if o == "task.done")
        assert "run" not in done_args and done_args["receipt_output"] == "-" and done_args["receipt"]["cmd"] == "true"
    finally:
        served.close()


def test_task_done_run_on_a_ticket_with_a_linked_repo_names_the_repo_and_its_commit(lab, monkeypatch):
    store = lab.ws.other()
    lab.ws.store = store
    try:
        path = lab.ws.repo("proj")
    finally:
        store.close()
        lab.ws.store = None
    commit = lab.ws.git("rev-parse", "HEAD")
    served = hosted(lab)
    try:
        c = hcli(lab)
        c("new", "A")
        c("claim", "1")
        c("set", "1", 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}')
        c("task", "add", "x", "--verify", "true")
        c("task", "start", "T1")
        monkeypatch.chdir(lab.ws.tmp)  # not inside the linked repo: refused, nothing sent
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (5, "invalid.input"), r.out
        assert _done_events(lab, "1") == []
        monkeypatch.chdir(path)
        (path / "dirty.txt").write_text("x")
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (5, "verify.failed") and "not clean" in r.out, r.out
        (path / "dirty.txt").unlink()
        r = c.j("task", "done", "T1", "--run")
        assert r.code == 0, r.out
        (done,) = _done_events(lab, "1")
        assert done["receipt"]["repo"] == "proj" and done["receipt"]["commit"] == commit
    finally:
        served.close()


def _registry(state, root_of):
    from orch.host.custody import ensure_private_dir, write_private
    from orch.host.registry import registry_path

    ensure_private_dir(registry_path(state).parent, UID)
    items = [
        {"workspace_id": wid, "prefix": "X", "root": root, "host_uid": UID, "group": "g",
         "peers": {str(UID): {"class": "person", "person": "p_" + "0" * 32}}, "repos": {}}
        for wid, root in root_of.items()
    ]  # fmt: skip
    write_private(registry_path(state), {"v": 1, "workspaces": items})


def _tree(path, wid):
    (path / ".orch").mkdir(parents=True)
    (path / ".orch" / "config.json").write_text(
        json.dumps({"schema": "orch.workspace/2", "workspace": {"id": wid, "name": "n"}})
    )
    return path


def test_a_config_json_naming_another_registered_workspace_is_not_routed_to_it(tmp_path):
    a_id, b_id = "a" * 32, "b" * 32
    a = _tree(tmp_path / "A", a_id)
    b = _tree(tmp_path / "B", b_id)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    _registry(state, {a_id: str(a), b_id: str(b)})
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(state)}
    from orch.ops.runtime import Workspace

    def locate(root):
        return hostclient.locate(env, Workspace(env | {"ORCH_WORKSPACE": str(root)}, files=_files()), state=state)

    assert locate(a).workspace_id == a_id and locate(b).workspace_id == b_id
    (a / ".orch" / "config.json").write_text(
        json.dumps({"schema": "orch.workspace/2", "workspace": {"id": b_id, "name": "n"}})
    )  # A's config now carries B's id
    with pytest.raises(hostclient.HostFailure) as caught:
        locate(a)
    assert (caught.value.code, caught.value.wire_exit) == ("host.unavailable", 9)
    assert str(b) in caught.value.message
    # an id that is not 32 hex characters never reaches a socket path
    (a / ".orch" / "config.json").write_text(
        json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "/tmp/x", "name": "n"}})
    )
    assert locate(a) is None


def test_a_socket_of_another_uid_is_host_untrusted_and_nothing_is_sent(lab):
    served = hosted(lab)
    try:
        target = hostclient.Target(served.wid, UID + 1, lab.run / served.wid / "host.sock", "person", lab.state, None)
        with pytest.raises(hostclient.HostFailure) as caught:
            hostclient._request(target, "status", {}, auth={"grant": "gr_SECRET"}, idem=None)
        assert (caught.value.code, caught.value.wire_exit) == ("host.untrusted", 3)
    finally:
        served.close()


def test_a_lost_submit_is_sent_again_with_the_same_idem_and_a_grant_exists_once(lab, monkeypatch):
    served = hosted(lab)
    try:
        owner = lab.ws.owner

        class Backend(KeyBackend):
            def public_key(self, key_id):
                return owner.sig_pub

        shown, secrets = [], []
        monkeypatch.setattr(ops_human, "open_backend", lambda directory: Backend(owner))
        monkeypatch.setattr(ops_human, "review_prompt", lambda text, word: shown.append(word) or True)
        monkeypatch.setattr(ops_human, "show_secret", lambda text: secrets.append(text))
        keys = ops_human.keys_dir(lab.state, served.wid)
        keys.mkdir(parents=True)
        (keys / ops_human.KEY_FILE).write_text("{}")
        real, seen = Connection.call, []

        def lossy(self, op, args, **kw):
            out = real(self, op, args, **kw)
            if op == "human.submit":
                seen.append(kw.get("idem"))
                if len(seen) == 1:
                    raise ConnectionError("the answer was lost")
            return out

        monkeypatch.setattr(Connection, "call", lossy)
        r = hcli(lab, grant=False).j("grant", "--label", "ci")
        assert r.code == 0, r.out
        assert len(seen) == 2 and seen[0] == seen[1] and seen[0].startswith("a_")
        assert len(secrets) == 1 and "ORCH_GRANT=" in secrets[0]
        # never silent: when every attempt is lost the idem is named
        seen.clear()
        monkeypatch.setattr(
            Connection,
            "call",
            lambda self, op, args, **kw: (
                (_ for _ in ()).throw(ConnectionError("gone")) if op == "human.submit" else real(self, op, args, **kw)
            ),
        )
        r = hcli(lab, grant=False).j("grant", "--label", "ci2")
        assert (r.code, r.err_code) == (9, "host.unavailable")
        assert "idem a_" in r.doc["error"]["message"] and "human.pending" in r.doc["error"]["message"]
    finally:
        served.close()


def test_a_blob_retry_names_the_digest_and_a_fresh_upload_drops_the_unused_blob():
    calls = []

    class Conn:
        def call(self, op, args, **kw):
            calls.append((op, args.get("blob")))
            return {"data": {"blob": "b_" + "A" * 26}}

    data = {"path": bytes(range(256)) * 3000}
    build = hostclient.files_builder(data, None)
    first = build(Conn(), "first")
    assert first["path"] == {"blob": "b_" + "A" * 26}
    assert build(Conn(), "digest")["path"]["digest"].startswith("sha256:")
    n = len(calls)
    build(Conn(), "fresh")
    assert ("blob.drop", "b_" + "A" * 26) in calls[n:]


def _lying_host(lab, monkeypatch, tasklist):
    """Replace the task.list answer; returns the list of commands the runner is asked to run."""
    from orch.ops import runner

    ran = []
    monkeypatch.setattr(runner, "run_verify", lambda cmd, **kw: ran.append(cmd) or (_ for _ in ()).throw(RuntimeError))
    orig = hostclient._request

    def fake(target, op, args, **kw):
        if op == "task.list":
            return tasklist
        return orig(target, op, args, **kw)

    monkeypatch.setattr(hostclient, "_request", fake)
    return ran


def _setup(lab):
    c = hcli(lab)
    c("new", "A")
    c("claim", "1")
    c("task", "add", "x", "--verify", "true")
    c("task", "start", "T1")
    return c


def test_a_malformed_or_missing_verify_from_the_host_is_never_run(lab, monkeypatch):
    served = hosted(lab)
    try:
        c = _setup(lab)
        bad = [None, "", "   ", 5, ["true"], {"cmd": "true"}, "true\x00", "x" * 5000]
        for value in bad:
            task = {"id": "T1", "text": "x", "state": "open", **({} if value is None else {"verify": value})}
            doc = {"key": "DEMO-0001", "data": {"tasks": [task], "repos": []}}
            ran = _lying_host(lab, monkeypatch, doc)
            r = c.j("task", "done", "T1", "--run")
            assert (r.code, r.err_code) == (5, "invalid.input"), (value, r.out)
            assert ran == [] and _done_events(lab, "1") == []
    finally:
        served.close()


def test_a_host_answer_for_another_task_or_ticket_runs_nothing(lab, monkeypatch):
    served = hosted(lab)
    try:
        c = _setup(lab)
        other_task = {
            "key": "DEMO-0001",
            "data": {"tasks": [{"id": "T2", "text": "x", "state": "open", "verify": "id"}]},
        }
        other_key = {
            "key": "DEMO-0002",
            "data": {"tasks": [{"id": "T1", "text": "x", "state": "open", "verify": "id"}]},
        }
        no_key = {"data": {"tasks": [{"id": "T1", "text": "x", "state": "open", "verify": "id"}]}}
        for doc, argv in ((other_task, ("T1",)), (other_key, ("DEMO-0001/T1",)), (no_key, ("T1",))):
            ran = _lying_host(lab, monkeypatch, doc)
            r = c.j("task", "done", *argv, "--run")
            assert r.code != 0, r.out
            assert ran == [] and _done_events(lab, "1") == []
    finally:
        served.close()


def test_the_task_done_goes_to_the_ticket_the_command_was_read_from(lab, monkeypatch):
    served = hosted(lab)
    try:
        c = _setup(lab)
        sent = []
        orig = hostclient._request

        def spy(target, op, args, **kw):
            sent.append((op if not kw.get("dry_run") else op + "?dry", dict(args)))
            return orig(target, op, args, **kw)

        monkeypatch.setattr(hostclient, "_request", spy)
        assert c("task", "done", "T1", "--run").code == 0
        assert [a["task"] for o, a in sent if o == "task.done"] == ["DEMO-0001/T1"]
        assert [a["task"] for o, a in sent if o == "task.done?dry"] == ["DEMO-0001/T1"]  # the preflight, same task
    finally:
        served.close()


def test_the_ticket_key_is_compared_canonically():
    for ref in ("DEMO-1", "demo-0001", "DEMO-0001", "1", "0001"):
        assert hostclient._bound_key("DEMO-0001", ref) == "DEMO-0001"
    for ref in ("DEMO-2", "OTHER-1", "2"):
        with pytest.raises(Exception, match="another ticket"):
            hostclient._bound_key("DEMO-0001", ref)


def test_every_signing_path_uses_one_device_key_lookup(tmp_path, monkeypatch):
    """The CLI's human ops, the pairing commands and the hosted Human find the key by ``ops.human.device_backend``:
    the passphrase file where init put it, else the convenience selection; none is ``custody.no_key``."""
    import pytest

    from orch.custody import select
    from orch.ops.errors import OrchError

    wid = "ab" * 16
    with pytest.raises(OrchError) as e:
        ops_human.device_backend(tmp_path, wid)
    assert e.value.code == "custody.no_key"
    marker = object()
    monkeypatch.setattr(ops_human, "open_backend", lambda directory: marker)
    keys = ops_human.keys_dir(tmp_path, wid)
    keys.mkdir(parents=True)
    (keys / ops_human.KEY_FILE).write_text("{}")
    assert ops_human.device_backend(tmp_path, wid) is marker
    (keys / ops_human.KEY_FILE).unlink()  # moved to convenience custody: the passphrase file is gone
    conv = object()
    monkeypatch.setattr(select, "read_selection", lambda s, w: {"custody": "convenience", "transition": None})
    monkeypatch.setattr(select, "device_backend", lambda s, w: (conv, None))
    assert ops_human.device_backend(tmp_path, wid, convenience=True) is conv


def test_a_key_in_convenience_custody_is_refused_outside_the_hosted_path(tmp_path, monkeypatch):
    """Direct mode and the genesis pin / device pairing are passphrase-only; only the hosted CLI path (the host checks
    the flag) may use the convenience backend."""
    import pytest

    from orch.custody import select
    from orch.ops.errors import OrchError

    wid = "ab" * 16
    conv = object()
    monkeypatch.setattr(select, "read_selection", lambda s, w: {"custody": "convenience", "transition": None})
    monkeypatch.setattr(select, "device_backend", lambda s, w: (conv, None))
    with pytest.raises(OrchError) as e:
        ops_human.device_backend(tmp_path, wid)  # what direct-mode Human.backend calls
    assert e.value.code == "custody.no_key" and "one-click custody" in e.value.message and e.value.hint
    assert ops_human.device_backend(tmp_path, wid, convenience=True) is conv  # hosted CLI path
