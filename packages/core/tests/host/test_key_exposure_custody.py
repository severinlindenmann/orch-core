# ruff: noqa: F811
"""#1097 E3 and the guided rotation, at the custody layer: ``select.stage_convenience`` signs and admits the
``device.exposed`` mark BEFORE any key-store write (and moves nothing when it cannot), ``exposure.mark_before_move``
signs with the opened scalar through the host, and ``rotate`` installs a new key only after ``device.rotated``
landed."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orch import crypto
from orch.custody import exposure, rotate, select
from orch.custody.base import CustodyError
from orch.custody.passphrase import KdfParams, PassphraseBackend
from orch.host import convenience
from orch.host.control_client import new_idem  # noqa: F401
from orch.ops.errors import OrchError
from tests.host import human_helpers
from tests.host.human_helpers import ok
from tests.host.lifecycle_helpers import UID

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
WORDS = "orbit candle meadow zipper falcon"
SRC = Path(select.__file__).resolve().parent


def device_of(served, ref):
    served.service.store.refresh()
    return served.service.store.state.workspace.devices[ref]


def call_for(served):
    c = served.client()

    def call(op, args, files=None, idem=None):
        return ok(c.req(op, args, files=files, idem=idem))

    return call


def entry_of(served):
    return convenience.registered_entry(served.lab.state, served.wid, UID)


# -- E3: the mark before the key moves ------------------------------------------------------------------------------


def test_mark_before_move_signs_with_the_opened_scalar_and_the_host_admits_it(served):
    owner = served.lab.ws.owner
    scalar, pub = crypto.private_scalar(owner.dk_sig), owner.sig_pub
    assert not device_of(served, owner.device).exposed
    exposure.mark_before_move(
        served.lab.state, served.wid, owner.device, scalar, pub, entry=entry_of(served), call=call_for(served)
    )
    assert device_of(served, owner.device).exposed
    seq = served.service.store.log_seq("workspace")
    exposure.mark_before_move(  # already marked: nothing more is written
        served.lab.state, served.wid, owner.device, scalar, pub, entry=entry_of(served), call=call_for(served)
    )
    assert served.service.store.log_seq("workspace") == seq


def test_mark_before_move_refuses_a_scalar_that_is_not_the_devices_key(served):
    owner = served.lab.ws.owner
    other = crypto.generate_private_key()
    with pytest.raises(CustodyError):
        exposure.mark_before_move(
            served.lab.state, served.wid, owner.device, crypto.private_scalar(other), owner.sig_pub,
            entry=entry_of(served), call=call_for(served),
        )  # fmt: skip
    assert not device_of(served, owner.device).exposed


def test_the_mark_backend_signs_one_workspace_event_and_nothing_else(served):
    owner = served.lab.ws.owner
    b = exposure._MarkBackend(crypto.private_scalar(owner.dk_sig), owner.sig_pub)
    with pytest.raises(CustodyError):
        b.sign("dk", b"orch/v2/sig/revocation|x", action="a")
    ws_label = __import__("orch.canon", fromlist=["LABELS"]).LABELS["sig_ws_event"].encode("ascii")
    assert len(b.sign("dk", ws_label + b"{}", action="a")) == 64
    with pytest.raises(CustodyError):
        b.sign("dk", ws_label + b"{}", action="a")  # one signature only


class FakeLegacy:
    def __init__(self, owner):
        self.owner = owner

    def public_key(self, key_id):
        return self.owner.sig_pub

    def export_for_custody_change(self, key_id, *, fields):
        return bytearray(crypto.private_scalar(self.owner.dk_sig))


class RecordingStore:
    def __init__(self, log):
        self.log = log

    def exists(self, *a):
        return False

    def put(self, *a, **k):
        self.log.append("put")


def test_mark_before_move_refuses_a_workspace_without_a_registry_entry(served):
    owner = served.lab.ws.owner
    with pytest.raises(CustodyError, match="not registered"):
        exposure.mark_before_move(
            served.lab.state, "ab" * 16, owner.device, crypto.private_scalar(owner.dk_sig), owner.sig_pub
        )


def test_stage_convenience_moves_nothing_for_an_unregistered_workspace(tmp_path, monkeypatch):
    class Owner:
        dk_sig = crypto.generate_private_key()
        sig_pub = crypto.public_bytes(dk_sig)

    order = []
    monkeypatch.setattr(select, "_legacy_backend", lambda *a, **k: FakeLegacy(Owner))
    monkeypatch.setattr(select, "_write_selection", lambda *a, **k: order.append("selection"))
    with pytest.raises(CustodyError, match="not registered"):
        select.stage_convenience(tmp_path, "cd" * 16, store=RecordingStore(order))
    assert order == []


def test_stage_convenience_unpatched_writes_the_mark_through_the_host_socket_before_the_key_store(
    served, old_key_on_disk, monkeypatch
):
    """The real path: registry entry, ``peers[uid]``, the workspace socket, prepare/sign/submit, then ``store.put``."""
    from orch.ops import human as ops_human

    owner = served.lab.ws.owner
    monkeypatch.setattr(ops_human, "open_backend", backend_over)
    monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(served.lab.run))
    seen = []

    class Store(RecordingStore):
        def put(self, *a, **k):
            seen.append(device_of(served, owner.device).exposed)  # the mark is in the log when the copy is made

    select.stage_convenience(served.lab.state, served.wid, store=Store([]))
    assert seen == [True]


def test_stage_convenience_marks_before_the_first_key_store_write(served, monkeypatch):
    owner = served.lab.ws.owner
    order = []
    monkeypatch.setattr(select, "_legacy_backend", lambda *a, **k: FakeLegacy(owner))
    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: order.append("mark"))
    monkeypatch.setattr(select, "_write_selection", lambda *a, **k: order.append("selection"))
    select.stage_convenience(served.lab.state, served.wid, store=RecordingStore(order))
    assert order == ["mark", "put", "selection"]


def test_stage_convenience_moves_nothing_when_the_mark_cannot_land(served, monkeypatch):
    owner = served.lab.ws.owner
    order = []

    def refuse(*a, **k):
        raise CustodyError("the exposure mark could not be written (host.unavailable); the key was not moved")

    monkeypatch.setattr(select, "_legacy_backend", lambda *a, **k: FakeLegacy(owner))
    monkeypatch.setattr(exposure, "mark_before_move", refuse)
    monkeypatch.setattr(select, "_write_selection", lambda *a, **k: order.append("selection"))
    with pytest.raises(CustodyError, match="not moved"):
        select.stage_convenience(served.lab.state, served.wid, store=RecordingStore(order))
    assert order == []  # no store.put, no transition written


def test_the_mark_call_precedes_every_key_store_write_in_stage_convenience():
    """The AST pin of the order: ``exposure.mark_before_move`` is called before ``store.put`` and the selection."""
    tree = ast.parse((SRC / "select.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "stage_convenience")
    lines: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            lines.setdefault(f"{owner}.{node.func.attr}", node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lines.setdefault(node.func.id, node.lineno)
    assert lines["exposure.mark_before_move"] < lines["store.put"] < lines["_write_selection"]
    assert lines["exposure.mark_before_move"] > lines["legacy.export_for_custody_change"]


# -- the guided rotation (the CLI side) -----------------------------------------------------------------------------


class FakePK:
    """A person-key source over the Lab owner's person key (the real one types the recovery code on /dev/tty)."""

    def __init__(self, owner):
        self.owner = owner
        self.pk_pub = owner.pk_pub

    def __call__(self, **kw):
        assert set(kw["labels"]) == {"sig_device_cert", "sig_device_rotate"}  # least privilege
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def signer(self, label):
        return self.owner.pk_sign

    def derive_label_key(self):
        return b"k" * 32


def keyfile_of(served):
    from orch.ops import human as ops_human

    return ops_human.keys_dir(served.lab.state, served.wid)


def backend_over(directory):
    return PassphraseBackend(Path(directory), _passphrase_provider=lambda r: WORDS, _kdf=KdfParams(2**15, 8, 1))


@pytest.fixture
def old_key_on_disk(served):
    """The owner's device key as a passphrase key file at the place ``orch setup`` puts it."""
    owner = served.lab.ws.owner
    directory = keyfile_of(served)
    directory.parent.mkdir(parents=True, exist_ok=True)
    backend_over(directory).create("dk", secret=crypto.private_scalar(owner.dk_sig), role="device", passphrase=WORDS)
    return directory


def run_rotate(served, **over):
    kw = dict(
        say=lambda t: None, pk_factory=FakePK(served.lab.ws.owner), open_backend=backend_over,
        confirm=lambda t: True, call=call_for(served),
    )  # fmt: skip
    kw.update(over)
    return rotate.rotate(served.lab.state, entry_of(served), {}, **kw)


def test_rotate_lands_device_rotated_then_installs_the_new_key(served, old_key_on_disk):
    owner = served.lab.ws.owner
    old_pub = backend_over(old_key_on_disk).public_key("dk")
    assert old_pub == owner.sig_pub
    out = run_rotate(served)
    assert out["old"] == owner.device and out["device"] != owner.device and out["custody"] == "passphrase"
    assert device_of(served, owner.device).removed
    assert not device_of(served, out["device"]).removed and not device_of(served, out["device"]).exposed
    installed = backend_over(old_key_on_disk).public_key("dk")
    assert installed != old_pub and installed != owner.sig_pub  # the new key is THE key here now
    assert not (old_key_on_disk.parent / rotate.STAGE).exists() or not any(
        (old_key_on_disk.parent / rotate.STAGE).iterdir()
    )


def test_rotate_leaves_the_old_key_in_place_when_the_event_does_not_land(served, old_key_on_disk):
    owner = served.lab.ws.owner
    with pytest.raises(OrchError, match="nothing was signed"):
        run_rotate(served, confirm=lambda t: False)
    assert backend_over(old_key_on_disk).public_key("dk") == owner.sig_pub
    assert not device_of(served, owner.device).removed


def test_rotate_refuses_while_the_key_is_in_convenience_custody(served, old_key_on_disk, monkeypatch):
    monkeypatch.setattr(convenience, "enabled", lambda *a, **k: True)
    with pytest.raises(CustodyError, match="one-click"):
        run_rotate(served)


def test_rotate_resumes_after_a_crash_between_the_event_and_the_install(served, old_key_on_disk, monkeypatch):
    owner = served.lab.ws.owner

    class Crash(Exception):
        pass

    def die(*a, **k):
        raise Crash

    monkeypatch.setattr(rotate, "_install", die)
    with pytest.raises(Crash):
        run_rotate(served)  # device.rotated landed, the new key was not installed
    assert device_of(served, owner.device).removed
    assert backend_over(old_key_on_disk).public_key("dk") == owner.sig_pub  # still the old (dead) key on disk
    monkeypatch.undo()
    out = run_rotate(served, pk_factory=None)  # the rerun needs no recovery code and signs nothing
    installed = backend_over(old_key_on_disk).public_key("dk")
    assert installed != owner.sig_pub and out["device"] != owner.device
    assert not device_of(served, out["device"]).removed
