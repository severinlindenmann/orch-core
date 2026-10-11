"""H3b3: ``host.bootstrap`` and the ``init`` composite (``orch init --host``).

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
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orch import crypto
from orch.crypto import suite
from orch.host.control_client import ControlClient, new_idem
from orch.host.service import Host
from orch.identity import derive_person_key
from orch.identity.recovery import code_from_entropy
from orch.ops import workspace_init as wi
from orch.ops.base import Context
from orch.ops.caps import Files
from orch.ops.commands.init import OP
from orch.ops.errors import OrchError
from orch.ops.runtime import Workspace
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


# --- 1. the same genesis bytes in both modes -------------------------------------------------------------------


def _deterministic(monkeypatch) -> dict[str, Any]:
    """Every random draw of an init made repeatable: keys (in draw order), ULIDs, the label salt, ECDSA nonces
    (RFC 6979), the recovery code and the workspace id. ``reset()`` starts the sequences again."""
    state = {"keys": 0, "ulids": 0}
    scalars = [int.from_bytes(hashlib.sha256(f"key {i}".encode()).digest(), "big") % (2**255) for i in range(8)]

    def generate_private_key():
        state["keys"] += 1
        return crypto.private_key_from_scalar(scalars[state["keys"] - 1])

    def new_ulid(*_a, **_k):
        state["ulids"] += 1
        return f"01J9ZP00000000000000000{state['ulids']:03d}"

    def sign(key, msg):
        r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256(), deterministic_signing=True)))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    monkeypatch.setattr(crypto, "generate_private_key", generate_private_key)
    monkeypatch.setattr(crypto, "sign", sign)
    monkeypatch.setattr(crypto, "salted_seal", lambda k, m, a, p: suite._salted_seal(k, m, a, p, b"\0" * 16))
    monkeypatch.setattr(wi, "new_ulid", new_ulid)
    monkeypatch.setattr(wi, "generate_recovery_code", lambda: CODE)
    monkeypatch.setattr(wi, "secrets", SimpleNamespace(token_hex=lambda n: WID, SystemRandom=wi.secrets.SystemRandom))
    monkeypatch.setattr(Host, "new_workspace_id", lambda self: WID)

    def reset():
        state["keys"] = state["ulids"] = 0

    return {"reset": reset}


def _files(root: Path, state: Path) -> dict[str, bytes]:
    return {
        "genesis line": (root / "events" / "workspace.jsonl").read_bytes(),
        "config.json": (root / "config.json").read_bytes(),
        "keys.jsonl": (root / "keys.jsonl").read_bytes(),
        "pin": (state / "hosts" / WID / "genesis").read_bytes(),
    }


def test_init_through_the_host_yields_the_same_genesis_bytes_as_direct_mode(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    seams = _deterministic(monkeypatch)
    hosted_root = tmp_path / "h" / "work"
    hosted_root.mkdir(parents=True)
    direct_root = tmp_path / "d" / "work"
    direct_root.mkdir(parents=True)
    direct_state = tmp_path / "dstate"
    args = {"prefix": "DEMO", "name": "Severin"}
    with lab.host(clock=lambda: T):
        seams["reset"]()
        hosted = wi.create_workspace_hosted(ctx_for(hosted_root, lab.state, lambda: T), dict(args, host=True))
    seams["reset"]()
    direct = wi.create_workspace(ctx_for(direct_root, direct_state, lambda: T), dict(args))
    assert hosted.data == direct.data == {"prefix": "DEMO", "workspace_id": WID} and hosted.seq == direct.seq == 1
    assert [f["op"] for f in recording.sent] == ["host.wsk_create", "host.bootstrap"]
    got, want = _files(hosted_root, lab.state), _files(direct_root, direct_state)
    assert got == want
    line = json.loads(got["genesis line"])
    assert line["type"] == "workspace.created" and line["seq"] == 1 and line["host_sig"]
    # the seams really pinned everything: without them the bytes differ (a guard against a vacuous comparison)
    assert crypto.b64u(crypto.public_bytes(derive_person_key(CODE))) == line["owner"]["pk_pub"]


# --- 2. the CLI composite: the client signs, the host never sees the person key --------------------------------


def _scan(base: Path, needles: dict[str, bytes], skip: tuple[Path, ...] = ()) -> list[tuple[str, str]]:
    found = []
    for p in base.rglob("*"):
        if p.is_file() and not p.is_symlink() and not any(p == s or s in p.parents for s in skip):
            data = p.read_bytes()
            found += [(name, str(p)) for name, needle in needles.items() if needle in data]
    return found


def test_cli_init_host_runs_the_composite_and_the_host_never_sees_the_person_key(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    from orch.cli.main import main

    root = fresh_root(tmp_path, "work")
    monkeypatch.chdir(root)
    env = {"ORCH_STATE_DIR": str(lab.state), "HOME": str(tmp_path / "home"), "USER": "severin"}
    out = io.StringIO()
    with lab.host() as h:
        code = main(
            ["init", "--prefix", "DEMO", "--name", "Severin", "--host", "--json"],
            env=env,
            stdout=out,
            stderr=io.StringIO(),
        )
        doc = json.loads(out.getvalue())
        assert code == 0 and doc["ok"] is True, doc
        wid = doc["data"]["workspace_id"]
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
        assert _scan(lab.state, person_secrets) == [] and _scan(root, person_secrets) == []
        # the workspace key: only its public half is ever sent back, and it is in no host file but its own
        d = json.loads((lab.state / "hosts" / wid / "keys" / "wsk.filekey.json").read_bytes())["d"]
        wsk = {"wsk b64": d.encode(), "wsk hex": crypto.unb64u(d).hex().encode()}
        received = json.dumps(recording.received).encode()
        assert [n for n, v in wsk.items() if v in received] == []
        assert recording.received[0]["result"]["data"]["wsk_pub"] == genesis["wsk_pub"]
        assert _scan(lab.state, wsk, skip=(lab.state / "hosts" / wid / "keys",)) == []
        assert _scan(root, wsk) == []
        # the workspace is the host's now: a direct-mode call answers host.required
        out = io.StringIO()
        assert main(["status", "--json"], env=env, stdout=out, stderr=io.StringIO()) == 3
        assert json.loads(out.getvalue())["error"]["code"] == "host.required"


def test_cli_init_host_refuses_a_non_empty_directory_before_calling_the_host(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    (root / "README.md").write_text("x")
    with pytest.raises(OrchError) as e:
        wi.create_workspace_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    assert e.value.code == "invalid.input" and "empty directory" in e.value.message
    assert recording.sent == [] and term.shown == []  # no host call, no secret shown


def test_cli_init_host_without_a_host_is_host_unavailable_and_leaves_nothing(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    before = sorted(os.listdir(lab.state / "hosts"))
    with pytest.raises(OrchError) as e:
        wi.create_workspace_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    assert e.value.code == "host.unavailable" and "nothing was created" in e.value.message
    assert os.listdir(root) == [] and sorted(os.listdir(lab.state / "hosts")) == before
    assert "host.unavailable" in {x["code"] for x in OP.errors}


def test_cli_init_host_passes_a_host_refusal_on_and_drops_its_device_key(
    lab, tmp_path, monkeypatch, term, passphrases, recording
):
    root = fresh_root(tmp_path, "work")
    root.chmod(0o777)  # writable by others: the host refuses it (D-P2-17)
    try:
        with lab.host(), pytest.raises(OrchError) as e:
            wi.create_workspace_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
    finally:
        root.chmod(0o700)
    assert e.value.code == "custody.unsafe_mode"
    wid = recording.received[0]["result"]["data"]["workspace_id"]
    assert not (lab.state / "hosts" / wid / "person").exists()  # the client's device key went with the refusal
    assert os.listdir(root) == []


# --- 3. bootstrap refusals: non-empty root, foreign owner, an id not from wsk_create ---------------------------


def test_bootstrap_refuses_a_non_empty_root_and_changes_nothing(lab, tmp_path):
    owner, root = Person(), fresh_root(tmp_path)
    (root / "keep.txt").write_text("x")
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        idem = new_idem()
        assert err(c.call("host.bootstrap", args_for(created, root, owner), idem=idem)) == "invalid.input"
        assert err(c.call("host.outcome", {"idem": idem})) == "outcome.unknown_idem"  # refused before the intent
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
    assert os.listdir(root) == ["keep.txt"]
    assert (lab.state / "hosts" / created["workspace_id"] / "keys" / "wsk.filekey.json").is_file()


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
        proc.wait(60)
        second["host"] = lab.host().__enter__()

    helper = threading.Thread(target=restart, daemon=True)
    try:
        wait_for_socket(path, proc)
        helper.start()
        res = wi.create_workspace_hosted(ctx_for(root, lab.state), {"prefix": "DEMO", "host": True})
        helper.join(30)
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
