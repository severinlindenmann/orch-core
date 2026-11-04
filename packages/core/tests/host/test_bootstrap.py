"""H3b3: ``host.bootstrap`` and the hosted create (``orch setup``).

Acceptance (orch-v2-host §8, H3b3):

* ``init`` through the host yields the same genesis bytes as direct mode, with the same inputs and keys;
* ``bootstrap`` refuses a non-empty root, a foreign owner and an id not from ``wsk_create``;
* a second ``bootstrap`` for an id that already has a genesis or a registration is ``host.busy`` (N5);
* the CLI ``init`` in host mode runs ``host.wsk_create`` + ``host.bootstrap``: the client signs the genesis and the
  host never sees the person key; nothing of the key material is in a response or a host file;
* a bootstrap the host rolled back at restart makes the composite start again from ``host.wsk_create``.

The crash points themselves (killed before or after the append) are in ``test_crash_recovery.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from orch import crypto
from orch.host.control_client import ControlClient, new_idem
from orch.identity import derive_person_key
from orch.identity.recovery import code_from_entropy
from orch.ops import workspace_init as wi
from orch.ops.base import Context
from orch.ops.caps import Files
from orch.ops.errors import OrchError
from orch.ops.runtime import Workspace
from tests.host.hosted_create import create_hosted
from tests.host.lifecycle_helpers import UID, Lab, genesis_for, host_process, wait_for_socket
from tests.identity.helpers import Person
from tests.instructions.conftest import FakeTerminal, Passphrases

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Unix host")

T = 1_790_000_000.0  # a fixed clock for the byte comparison
WID = "5" * 32
CODE = code_from_entropy(hashlib.sha256(b"the recovery code").digest())


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def term(monkeypatch) -> FakeTerminal:
    t = FakeTerminal()
    monkeypatch.setattr(wi, "TERMINAL", t)
    return t


@pytest.fixture
def passphrases(monkeypatch, term) -> Passphrases:
    from orch.custody import KdfParams, PassphraseBackend

    p = Passphrases(term)
    monkeypatch.setattr(
        wi, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=p, _kdf=KdfParams(2**15, 8, 1))
    )
    return p


class Recording(ControlClient):
    """The control client the composite uses, keeping every frame it sends and receives."""

    sent: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []

    def send(self, op, args=None, *, idem=None, **extra):
        Recording.sent.append({"op": op, "args": args, "idem": idem})
        return super().send(op, args, idem=idem, **extra)

    def read(self, timeout=None):
        frame = super().read(timeout)
        Recording.received.append(frame)
        return frame


@pytest.fixture
def recording(monkeypatch, lab):
    Recording.sent, Recording.received = [], []
    monkeypatch.setattr(wi, "connect_host", lambda sd: Recording(lab.run / "control" / "host.sock"))
    return Recording


def ok(res):
    assert res["t"] == "res" and res["ok"] is True, res
    return res["result"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


def ctx_for(root: Path, state: Path, now=time.time) -> Context:
    env = {"ORCH_STATE_DIR": str(state), "HOME": str(root.parent / "home"), "USER": "severin"}
    return Context(
        env=env,
        grant=None,
        human_presence=True,
        now=now,
        workspace=Workspace(env, now, cwd=str(root), files=Files.direct()),
    )


def args_for(created, root, owner, **kw):
    return {
        "workspace_id": created["workspace_id"],
        "root": str(root),
        "events": [kw.pop("event", None) or genesis_for(owner, created["workspace_id"], created["wsk_pub"])],
        "peers": {str(UID): {"class": "person", "person": owner.ref}},
    }


def fresh_root(tmp_path: Path, name: str = "fresh") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


# --- 2. the CLI composite: the client signs, the host never sees the person key --------------------------------


def _scan(base: Path, needles: dict[str, bytes], skip: tuple[Path, ...] = ()) -> list[tuple[str, str]]:
    found = []
    for p in base.rglob("*"):
        if p.is_file() and not p.is_symlink() and not any(p == s or s in p.parents for s in skip):
            data = p.read_bytes()
            found += [(name, str(p)) for name, needle in needles.items() if needle in data]
    return found


def test_hosted_create_runs_the_composite_and_the_host_never_sees_the_person_key(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    from orch.cli.main import main

    root = fresh_root(tmp_path, "work")
    monkeypatch.chdir(root)
    env = {"ORCH_STATE_DIR": str(lab.state), "HOME": str(tmp_path / "home"), "USER": "severin"}
    with lab.host() as h:
        res = create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "name": "Severin", "host": True})
        wid = res.data["workspace_id"]
        # exactly the composite, and the host drew the id
        assert [f["op"] for f in recording.sent] == ["host.wsk_create", "host.bootstrap"]
        bootstrap = recording.sent[1]["args"]
        assert bootstrap["workspace_id"] == wid and bootstrap["root"] == os.path.realpath(root)
        genesis = bootstrap["events"][0]
        assert genesis["type"] == "workspace.created" and "host_sig" not in genesis and genesis["sig"]
        assert bootstrap["peers"] == {str(UID): {"class": "person", "person": genesis["owner"]["person"]}}
        with h.client() as c:
            rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["prefix"], r["state"], r["person"]) for r in rows] == [
            (wid, "DEMO", "ok", genesis["owner"]["person"])
        ]
        # the device key stays with the client, next to the host's directory, as in direct mode
        assert (lab.state / "hosts" / wid / "person" / "dk.key.json").is_file()
        assert (root / "AGENTS.md").is_file() and (root / "CLAUDE.md").is_file()  # the instructions, client side

        # secrets: the person key, the recovery code and the passphrase never reach the host
        pk = derive_person_key(term.code)
        scalar = crypto.private_scalar(pk)
        person_secrets = {
            "pk scalar b64": crypto.b64u(scalar).encode(),
            "pk scalar hex": scalar.hex().encode(),
            "code": " ".join(term.code.split()[:4]).encode(),
            "passphrase": term.generated.encode(),
        }
        sent = json.dumps(recording.sent).encode()
        assert [n for n, v in person_secrets.items() if v in sent] == []
        # run-9 feedback 6: the client keeps the person key for one-click hub steps (pkvault): the one allowed place
        assert [h for h in _scan(lab.state, person_secrets) if "/person-key/" not in str(h[1])] == []
        assert _scan(root, person_secrets) == []
        # the workspace key: only its public half is ever sent back, and it is in no host file but its own
        d = json.loads((lab.state / "hosts" / wid / "keys" / "wsk.filekey.json").read_bytes())["d"]
        wsk = {"wsk b64": d.encode(), "wsk hex": crypto.unb64u(d).hex().encode()}
        received = json.dumps(recording.received).encode()
        assert [n for n, v in wsk.items() if v in received] == []
        assert recording.received[0]["result"]["data"]["wsk_pub"] == genesis["wsk_pub"]
        assert _scan(lab.state, wsk, skip=(lab.state / "hosts" / wid / "keys",)) == []
        assert _scan(root, wsk) == []
        # the workspace is the host's now: the CLI is a host client; no host here is host.unavailable, never direct
        out = io.StringIO()
        assert main(["status", "--json"], env=env, stdout=out, stderr=io.StringIO()) == 9
        assert json.loads(out.getvalue())["error"]["code"] == "host.unavailable"


def test_cli_init_host_refuses_a_non_empty_directory_before_calling_the_host(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    (root / ".orch").mkdir()
    with pytest.raises(OrchError) as e:
        create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    assert e.value.code == "invalid.input" and "own .orch/" in e.value.message
    assert recording.sent == [] and term.shown == []  # no host call, no secret shown


def test_cli_init_host_without_a_host_is_host_unavailable_and_leaves_nothing(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    before = sorted(os.listdir(lab.state / "hosts"))
    with pytest.raises(OrchError) as e:
        create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    assert e.value.code == "host.unavailable" and "nothing was created" in e.value.message
    assert os.listdir(root) == [] and sorted(os.listdir(lab.state / "hosts")) == before


def test_cli_init_host_passes_a_host_refusal_on_and_drops_its_device_key(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    root.chmod(0o777)  # writable by others: the host refuses it (D-P2-17)
    try:
        with lab.host(), pytest.raises(OrchError) as e:
            create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    finally:
        root.chmod(0o700)
    assert e.value.code == "custody.unsafe_mode"
    wid = recording.received[0]["result"]["data"]["workspace_id"]
    assert not (lab.state / "hosts" / wid / "person").exists()  # the client's device key went with the refusal
    assert os.listdir(root) == []


# --- 3. bootstrap refusals: non-empty root, foreign owner, an id not from wsk_create ---------------------------


def test_bootstrap_refuses_a_root_that_holds_its_own_dot_orch_and_changes_nothing(lab, tmp_path):
    owner, root = Person(), fresh_root(tmp_path)
    (root / "keep.txt").write_text("x")
    (root / ".orch").mkdir()
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        idem = new_idem()
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=idem)) == "invalid.input"
        assert err(c.call("host.outcome", {"idem": idem})) == "outcome.unknown_idem"  # refused before the intent
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
    assert sorted(os.listdir(root)) == [".orch", "keep.txt"] and os.listdir(root / ".orch") == []
    assert (lab.state / "hosts" / created["workspace_id"] / "keys" / "wsk.filekey.json").is_file()


def test_bootstrap_accepts_an_existing_project_and_rollback_keeps_its_files(lab, tmp_path, monkeypatch):
    from orch.store import StoreError, store

    owner, root = Person(), fresh_root(tmp_path)
    (root / "README.md").write_text("mine")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x")
    real_append = store.Store.append
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]

        def boom(self, *a, **k):
            raise StoreError("chain.broken", "no")

        monkeypatch.setattr(store.Store, "append", boom)
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem())) == "chain.broken"
        assert sorted(os.listdir(root)) == ["README.md", "src"]  # only the store's own names went
        assert (root / "src" / "a.py").read_text() == "x"
        monkeypatch.setattr(store.Store, "append", real_append)
        created = ok(c.call("host.wsk_create"))["data"]
        ok(c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem()))
    assert (root / ".orch" / "config.json").is_file() and (root / "README.md").read_text() == "mine"


def test_bootstrap_refuses_a_root_owned_by_another_uid(lab, tmp_path, monkeypatch):
    owner, root = Person(), fresh_root(tmp_path)
    target = os.stat(root)
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) != (target.st_dev, target.st_ino):
            return st
        fields = list(st[:10])
        fields[4] = UID + 4242  # st_uid: a foreign owner
        return os.stat_result(fields)

    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        monkeypatch.setattr(os, "fstat", fstat)
        res = c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem())
        monkeypatch.setattr(os, "fstat", real_fstat)
        assert err(res) == "custody.unsafe_mode" and "owner" in res["error"]["message"]
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
    assert os.listdir(root) == []


def test_bootstrap_refuses_a_root_below_an_ancestor_others_can_write(lab, tmp_path):
    owner = Person()
    shared = tmp_path / "shared"
    shared.mkdir()
    root = fresh_root(shared)
    shared.chmod(0o777)
    try:
        with lab.host() as h, h.client() as c:
            created = ok(c.call("host.wsk_create"))["data"]
            res = c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem())
            assert err(res) == "custody.unsafe_mode" and "writable by another principal" in res["error"]["message"]
    finally:
        shared.chmod(0o700)
    assert os.listdir(root) == []


def test_bootstrap_refuses_a_genesis_whose_owner_is_not_the_mapped_person(lab, tmp_path):
    owner, other, root = Person(), Person(), fresh_root(tmp_path)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        args = args_for(created, root, owner)
        foreign = {**args, "peers": {str(UID): {"class": "person", "person": other.ref}}}
        assert err(c.call("host.bootstrap", foreign, idem=new_idem())) == "op.not_allowed"
        # a genesis signed by someone else for this id and key: its owner maps no peer
        signed_by_other = {**args, "events": [genesis_for(other, created["workspace_id"], created["wsk_pub"])]}
        assert err(c.call("host.bootstrap", signed_by_other, idem=new_idem())) == "op.not_allowed"
    assert os.listdir(root) == []


@pytest.mark.parametrize("which", ["never created", "direct-mode keys", "another wsk_create"])
def test_bootstrap_refuses_an_id_or_key_not_from_wsk_create(lab, tmp_path, which):
    owner, root = Person(), fresh_root(tmp_path)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        if which == "never created":
            args = args_for({**created, "workspace_id": "e" * 32}, root, owner)
            want = "not_found"
        elif which == "direct-mode keys":
            # a key directory that direct-mode init made (no wsk_create marker, no genesis yet): not the host's
            wid = "d" * 32
            keys = lab.state / "hosts" / wid / "keys"
            keys.mkdir(parents=True, mode=0o700)
            from orch.custody import FileBackend

            pub = FileBackend(keys).create("wsk")
            args = args_for({"workspace_id": wid, "wsk_pub": crypto.b64u(pub)}, root, owner)
            want = "not_found"
        else:
            # the id of one wsk_create with the key of another: the genesis names a key the host did not draw for it
            second = ok(c.call("host.wsk_create"))["data"]
            event = genesis_for(owner, created["workspace_id"], second["wsk_pub"])
            args = args_for(created, root, owner, event=event)
            want = "invalid.input"
        assert err(c.call("host.bootstrap", args, idem=new_idem())) == want
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
    assert os.listdir(root) == []


def test_bootstrap_answers_the_genesis_model_codes_and_leaves_the_id_usable(lab, tmp_path):
    owner, root = Person(), fresh_root(tmp_path)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        event = genesis_for(owner, created["workspace_id"], created["wsk_pub"])
        forged = {**event, "owner": {**event["owner"], "name": "Mallory"}}  # changed after signing
        idem = new_idem()
        assert err(c.call("host.bootstrap", args_for(created, root, owner, event=forged), idem=idem)) == "sig.invalid"
        assert ok(c.call("host.outcome", {"idem": idem}))["data"]["error"]["code"] == "sig.invalid"
        assert os.listdir(root) == []  # the store's files went with the refusal
        assert not (lab.state / "hosts" / created["workspace_id"] / "genesis").exists()
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
        # the id and its key are still the client's to use with a good genesis
        result = ok(c.call("host.bootstrap", args_for(created, root, owner, event=event), idem=new_idem()))
        assert result["data"] == {"workspace_id": created["workspace_id"], "seq": 1}


# --- 4. N5: a second bootstrap of an id with a genesis or a registration, or of a root in progress ------------


def test_a_second_bootstrap_of_an_id_with_a_genesis_is_host_busy_even_after_unregister(lab, tmp_path):
    owner, root = Person(), fresh_root(tmp_path)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        ok(c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem()))
        other_root = fresh_root(tmp_path, "other")
        res = c.call("host.bootstrap", args_for(created, other_root, owner), idem=new_idem())
        assert err(res) == "host.busy" and res["exit"] == 4  # registered (and with a genesis)
        ok(c.call("host.unregister", {"workspace_id": created["workspace_id"]}, idem=new_idem()))
        res = c.call("host.bootstrap", args_for(created, other_root, owner), idem=new_idem())
        assert err(res) == "host.busy"  # no registration any more, but the genesis pin stays
        assert os.listdir(other_root) == []


def test_a_bootstrap_of_a_root_another_bootstrap_holds_is_host_busy(lab, tmp_path):
    owner, root = Person(), fresh_root(tmp_path)
    with lab.host() as h, h.client() as c:
        first = ok(c.call("host.wsk_create"))["data"]
        second = ok(c.call("host.wsk_create"))["data"]
        with h.host.bootstrap_guard(first["workspace_id"], os.path.realpath(root)):  # the first one is under way
            res = c.call("host.bootstrap", args_for(second, root, owner), idem=new_idem())
            assert err(res) == "host.busy" and "being bootstrapped" in res["error"]["message"]
            reg = c.call("host.register", {"root": str(root), "peers": lab.peers(owner.ref)}, idem=new_idem())
            assert err(reg) == "host.busy"
        ok(c.call("host.bootstrap", args_for(second, root, owner), idem=new_idem()))


# --- 5. the composite after a rolled-back bootstrap: again from wsk_create ------------------------------------


def test_the_composite_starts_again_from_wsk_create_after_a_rolled_back_bootstrap(
    lab, tmp_path, monkeypatch, term, passphrases
):
    """The host dies before the append (SIGKILL), a new host rolls the intent back; the composite reconnects, gets
    ``control.rolled_back`` for its idem, asks the person once, and creates a new id and key."""
    root = fresh_root(tmp_path, "work")
    path = lab.run / "control" / "host.sock"
    sent: list[dict[str, Any]] = []

    class Client(ControlClient):
        def send(self, op, args=None, *, idem=None, **extra):
            sent.append({"op": op, "workspace_id": (args or {}).get("workspace_id"), "idem": idem})
            return super().send(op, args, idem=idem, **extra)

    monkeypatch.setattr(wi, "connect_host", lambda sd: Client(path))
    proc = host_process(lab.state, lab.run, crash="bootstrap-before-append", pins=lab.pins_dir)
    second: dict[str, Any] = {}

    def restart() -> None:
        try:
            proc.wait(60)
            second["host"] = lab.host().__enter__()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread after join
            second["error"] = exc

    helper = threading.Thread(target=restart, daemon=True)
    try:
        wait_for_socket(path, proc)
        helper.start()
        res = create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
        helper.join(30)
        if "error" in second:
            raise second["error"]
        assert proc.returncode == -9
        host = second["host"].host
        assert host.recovered == [("host.bootstrap", "failed")]
        ops = [s["op"] for s in sent]
        assert ops[:2] == ["host.wsk_create", "host.bootstrap"] and ops[-2:] == ["host.wsk_create", "host.bootstrap"]
        first_wid, first_idem = sent[1]["workspace_id"], sent[1]["idem"]
        # the same idem was asked again after the restart: it got the stored control.rolled_back, never a second run
        assert [s["idem"] for s in sent if s["op"] == "host.bootstrap"].count(first_idem) >= 2
        assert res.data["workspace_id"] == sent[-1]["workspace_id"] != first_wid
        assert any("rolled the new workspace back" in w for w in term.waits)  # one confirmation
        assert not (lab.state / "hosts" / first_wid).exists()  # the old id and key are gone, its device key too
        assert (lab.state / "hosts" / res.data["workspace_id"] / "person" / "dk.key.json").is_file()
        with second["host"].client() as c:
            rows = ok(c.call("host.list"))["data"]["workspaces"]
            assert [(r["workspace_id"], r["state"]) for r in rows] == [(res.data["workspace_id"], "ok")]
            outcome = ok(c.call("host.outcome", {"idem": first_idem}))["data"]
            assert outcome["error"]["code"] == "control.rolled_back"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        helper.join(30)
        if "host" in second:
            second["host"].stop()


# --- 6. review round (#388): an unknown outcome is never "nothing was created" ----------------------------------


def _once(real, exc):
    """``real`` (a function, also as a method) failing its first call with ``exc``."""
    left = [1]

    def wrapped(*a, **k):
        if left:
            left.pop()
            raise exc
        return real(*a, **k)

    return wrapped


def _fake_clock(monkeypatch, seconds=1.0):
    now = [0.0]
    monkeypatch.setattr(wi, "_now", lambda: now[0])
    monkeypatch.setattr(wi, "_sleep", lambda d: now.__setitem__(0, now[0] + d))
    monkeypatch.setattr(wi, "RECONNECT_SECONDS", seconds)
    return now


def _assert_kept_and_unvoided(lab, wid, message):
    assert (lab.state / "hosts" / wid / "person" / "dk.key.json").is_file()  # the device key is kept
    assert "void" not in message and "nothing was created" not in message


@pytest.mark.parametrize("failure", ["lock.busy", "RuntimeError in registry._add"])
def test_a_failure_after_the_append_is_retried_with_the_same_idem_and_the_device_key_is_kept(
    lab, tmp_path, monkeypatch, term, passphrases, recording, failure
):
    """Opus F1 repro: finishing the bootstrap fails once after the genesis landed. The host answers outcome.unknown,
    the client asks again with the same idem, and the workspace ends registered with its device key."""
    from orch.host import control, registry
    from orch.host.errors import HostError
    from orch.host.ownership import OwnershipLock

    if failure == "lock.busy":
        busy = HostError("lock.busy", "held by pid 1", retry_after_ms=1000)

        class Flaky(OwnershipLock):
            pass

        Flaky.exclusive = _once(OwnershipLock.exclusive, busy)
        monkeypatch.setattr(control, "OwnershipLock", Flaky)
    else:
        monkeypatch.setattr(registry.Registry, "_add", _once(registry.Registry._add, RuntimeError("disk full")))
    root = fresh_root(tmp_path, "work")
    with lab.host() as h:
        res = create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
        wid = res.data["workspace_id"]
        bootstraps = [f for f in recording.sent if f["op"] == "host.bootstrap"]
        assert len({f["idem"] for f in bootstraps}) == 1 and len(bootstraps) >= 2  # the same idem, asked again
        first_answer = next(f for f in recording.received if not f.get("ok"))
        assert first_answer["error"]["code"] == "outcome.unknown"  # never the raw cause
        with h.client() as c:
            assert [r["workspace_id"] for r in ok(c.call("host.list"))["data"]["workspaces"]] == [wid]
    _assert_kept_and_unvoided(lab, wid, " ".join(res.lines))


def test_an_outcome_that_stays_unknown_is_host_unavailable_naming_the_idem(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    from orch.host import control
    from orch.host.errors import HostError
    from orch.host.ownership import OwnershipLock

    class Busy(OwnershipLock):
        def exclusive(self, wait):
            raise HostError("lock.busy", "held", retry_after_ms=1000)

    monkeypatch.setattr(control, "OwnershipLock", Busy)
    now = _fake_clock(monkeypatch, 1.0)
    root = fresh_root(tmp_path, "work")
    with lab.host() as h:
        with pytest.raises(OrchError) as e:
            create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
        assert now[0] >= 1.0  # it waited out the (fake) deadline
        idem = next(f["idem"] for f in recording.sent if f["op"] == "host.bootstrap")
        wid = next(f["args"]["workspace_id"] for f in recording.sent if f["op"] == "host.bootstrap")
        assert (
            e.value.code == "host.unavailable"
            and idem in e.value.hint
            and e.value.fix == ["orch", "host", "outcome", idem]
        )
        _assert_kept_and_unvoided(lab, wid, e.value.message)
        monkeypatch.setattr(control, "OwnershipLock", OwnershipLock)
        with h.client() as c:  # once the lock is free, the same idem finishes it
            assert ok(c.call("host.bootstrap", recording.sent[-2]["args"], idem=idem))["data"]["seq"] == 1
            assert [r["workspace_id"] for r in ok(c.call("host.list"))["data"]["workspaces"]] == [wid]


def test_ctrl_c_during_bootstrap_keeps_the_device_key_and_names_the_idem(lab, tmp_path, monkeypatch, term, passphrases):
    class Interrupted(Recording):
        def send(self, op, args=None, *, idem=None, **extra):
            request_id = super().send(op, args, idem=idem, **extra)
            if op == "host.bootstrap":
                raise KeyboardInterrupt  # the request is on its way; its answer is never read
            return request_id

    Recording.sent, Recording.received = [], []
    monkeypatch.setattr(wi, "connect_host", lambda sd: Interrupted(lab.run / "control" / "host.sock"))
    root = fresh_root(tmp_path, "work")
    with lab.host(), pytest.raises(OrchError) as e:
        create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    idem = Recording.sent[-1]["idem"]
    wid = Recording.sent[-1]["args"]["workspace_id"]
    assert e.value.code == "host.unavailable" and idem in e.value.hint
    _assert_kept_and_unvoided(lab, wid, e.value.message)


def test_a_connection_that_never_comes_back_ends_after_the_reconnect_deadline(
    lab, tmp_path, monkeypatch, term, passphrases
):
    connects: list[int] = []

    class Dropped(Recording):
        def send(self, op, args=None, *, idem=None, **extra):
            Recording.sent.append({"op": op, "args": args, "idem": idem})
            if op == "host.bootstrap":
                raise ConnectionResetError("the host went away")
            return ControlClient.send(self, op, args, idem=idem, **extra)

    def connect(sd):
        connects.append(1)
        if len(connects) > 1:
            raise FileNotFoundError("no socket")
        return Dropped(lab.run / "control" / "host.sock")

    Recording.sent, Recording.received = [], []
    monkeypatch.setattr(wi, "connect_host", connect)
    now = _fake_clock(monkeypatch, 30.0)
    root = fresh_root(tmp_path, "work")
    with lab.host(), pytest.raises(OrchError) as e:
        create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    assert 30.0 <= now[0] < 31.0 and len(connects) > 100  # retried every 0.25 s until the 30 s deadline
    idem = next(f["idem"] for f in Recording.sent if f["op"] == "host.bootstrap")
    wid = next(f["args"]["workspace_id"] for f in Recording.sent if f["op"] == "host.bootstrap")
    assert e.value.code == "host.unavailable" and idem in e.value.hint
    _assert_kept_and_unvoided(lab, wid, e.value.message)


# --- F2: the root is one directory whatever its spelling --------------------------------------------------------


def _variant(path: Path, kind: str) -> Path:
    if kind == "case":
        alt = path.parent / path.name.swapcase()
    else:
        import unicodedata

        alt = path.parent / unicodedata.normalize("NFD", path.name)
    if str(alt) == str(path) or not alt.exists() or not os.path.samefile(alt, path):
        pytest.skip(f"this volume does not fold {kind} variants of a name")
    return alt


@pytest.mark.parametrize("kind", ["case", "nfd"])
def test_the_root_guard_and_by_root_compare_directories_not_spellings(lab, tmp_path, kind):
    owner = Person()
    root = fresh_root(tmp_path, "work" if kind == "case" else "café")
    alt = _variant(root, kind)
    with lab.host() as h, h.client() as c:
        first = ok(c.call("host.wsk_create"))["data"]
        second = ok(c.call("host.wsk_create"))["data"]
        with h.host.bootstrap_guard(first["workspace_id"], os.path.realpath(root)):
            res = c.call("host.bootstrap", args_for(second, alt, owner), idem=new_idem())
            assert err(res) == "host.busy" and "being bootstrapped" in res["error"]["message"]
        ok(c.call("host.bootstrap", args_for(first, root, owner), idem=new_idem()))
        res = c.call("host.bootstrap", args_for(second, alt, owner), idem=new_idem())
        assert err(res) == "host.busy" and "already registered" in res["error"]["message"]  # by_root


def test_a_root_inside_the_state_dir_is_refused_under_another_spelling(lab, tmp_path):
    owner = Person()
    inside = lab.state / "x"
    inside.mkdir()
    alt_state = _variant(lab.state, "case")
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        res = c.call("host.bootstrap", args_for(created, alt_state / "x", owner), idem=new_idem())
        assert err(res) == "invalid.input" and "inside the host" in res["error"]["message"]


# --- F3: the live failure path keeps what it cannot prove its own ------------------------------------------------


def test_genesis_landed_is_unknown_when_the_log_cannot_be_read(tmp_path):
    from orch.host.control import ControlOps

    root = tmp_path / "r"
    (root / ".orch" / "events" / "workspace.jsonl").mkdir(
        parents=True
    )  # opening it fails with an OSError other than ENOENT
    assert ControlOps._genesis_landed(str(root), "x") is None
    assert ControlOps._genesis_landed(str(tmp_path / "missing"), "x") is False


def test_an_append_failure_that_cannot_be_judged_keeps_the_files_and_is_outcome_unknown(lab, tmp_path, monkeypatch):
    from orch.host.control import ControlOps
    from orch.store import store

    owner, root = Person(), fresh_root(tmp_path)

    def append(self, *a, **k):
        (root / ".orch" / "config.json").write_text("{}")
        raise RuntimeError("EIO")

    monkeypatch.setattr(store.Store, "append", append)
    monkeypatch.setattr(ControlOps, "_genesis_landed", staticmethod(lambda r, i: None))
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        idem = new_idem()
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=idem)) == "outcome.unknown"
        assert ok(c.call("host.outcome", {"idem": idem}))["data"] == {"state": "pending"}
    assert (root / ".orch" / "config.json").is_file()  # nothing deleted on a guess


def test_the_live_failure_path_leaves_a_root_another_init_took(lab, tmp_path, monkeypatch):
    from orch.store import StoreError, store

    owner, root = Person(), fresh_root(tmp_path)
    foreign = {"workspace": {"id": "f" * 32}}

    def append(self, *a, **k):  # another workspace's config appears in the store folder while the bootstrap runs
        (root / ".orch" / "config.json").write_text(json.dumps(foreign))
        raise StoreError("chain.broken", "the log changed on disk")

    monkeypatch.setattr(store.Store, "append", append)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem())) == "chain.broken"
    assert json.loads((root / ".orch" / "config.json").read_text()) == foreign


# --- F4: the key sweep and a bootstrap of the same id never overlap ---------------------------------------------


def test_the_key_sweep_skips_an_id_being_bootstrapped_and_deletes_it_afterwards(lab):
    now = [T]
    with lab.host(clock=lambda: now[0]) as h, h.client() as c:
        wid = ok(c.call("host.wsk_create"))["data"]["workspace_id"]
        now[0] += 3601
        with h.host.bootstrap_guard(wid):
            ok(c.call("host.wsk_create"))  # sweeps: the id in flight is kept
            assert (lab.state / "hosts" / wid / "keys").is_dir()
        ok(c.call("host.wsk_create"))
        assert not (lab.state / "hosts" / wid).exists()


def test_rollback_leaves_a_directory_the_user_made_after_validation(lab, tmp_path, monkeypatch):
    from orch.store import StoreError, store

    owner, root = Person(), fresh_root(tmp_path)

    def append(self, *a, **k):  # the user replaces .orch with their own folder while the bootstrap runs
        (root / ".orch").rename(tmp_path / "moved-away")
        (root / ".orch").mkdir()
        (root / ".orch" / "mine.txt").write_text("keep")
        raise StoreError("chain.broken", "no")

    monkeypatch.setattr(store.Store, "append", append)
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=new_idem())) == "chain.broken"
    assert (root / ".orch" / "mine.txt").read_text() == "keep"
    assert sorted(os.listdir(root)) == [".orch"]


def test_clean_root_removes_only_recorded_entries(tmp_path):
    from orch.host.control import ControlOps as Control

    root = tmp_path / "r"
    root.mkdir()
    (root / "events").mkdir()
    (root / "tickets").mkdir()
    (root / "tickets" / "mine").write_text("x")
    recorded = {"events": [os.lstat(root / "events").st_dev, os.lstat(root / "events").st_ino]}
    Control._clean_root(str(root), recorded, None)
    assert not (root / "events").exists() and (root / "tickets" / "mine").exists()


def test_connect_host_honours_the_run_directory_of_the_environment(tmp_path, monkeypatch):
    """#1193: setup and the hosted create find the host where every other host command does."""
    seen = []

    class Recorder:
        def __init__(self, path, timeout=None):
            seen.append(Path(path))

    monkeypatch.setattr("orch.host.control_client.ControlClient", Recorder)
    state, run = tmp_path / "state", tmp_path / "elsewhere"
    wi.connect_host(state, {"ORCH_HOST_RUN_DIR": str(run)})
    wi.connect_host(state, {})
    monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(run))
    wi.connect_host(state)
    assert seen == [
        run / "control" / "host.sock",
        state / "run" / "control" / "host.sock",
        run / "control" / "host.sock",
    ]


def test_a_rerun_after_a_failed_genesis_reuses_the_half_made_workspace(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    # run-11 item 6: every `orch setup` rerun after a failure drew a new workspace id
    from orch.custody.base import CustodyError
    from orch.ops import workspace_init as wi

    root = fresh_root(tmp_path, "work")
    monkeypatch.chdir(root)
    real = wi._signed_genesis
    calls = []

    def flaky(*a, **kw):
        calls.append(a[2])
        if len(calls) == 1:
            raise CustodyError("person is writable by others")
        return real(*a, **kw)

    monkeypatch.setattr(wi, "_signed_genesis", flaky)
    with lab.host():
        with pytest.raises(Exception):  # noqa: B017
            create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "name": "Severin", "host": True})
        res = create_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "name": "Severin", "host": True})
        assert calls[0] == calls[1] == res.data["workspace_id"]
        assert [f["op"] for f in recording.sent].count("host.wsk_create") == 1
