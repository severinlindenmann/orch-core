"""wk_seal (protocol.md §10.2): full-mode workspaces hand WK_e, sealed to HXK, to the hub. A real TLS link to a real
LinkServer; the wrap is built exactly as orch-core builds it (``orch.crypto.sealing.seal``: purpose ``wk``, recipient
= hub id, object = workspace id, epoch)."""

from __future__ import annotations

import os
import pathlib
import secrets
import sqlite3
import stat

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.inspect import inspect as hub_inspect
from orch_hub.keys import HubKeys
from orch_hub.link import LinkError
from orch_hub.link.wkseal import WkSeals

HXK_PRIV = G.K.priv["hxk"]
HXK_PUB = G.K.pub["hxk"]


def core_seal(wk: bytes, *, hub_id=G.HUB_ID, ws=G.WS_A, epoch=1, to=HXK_PUB) -> bytes:
    """Port of orch-core ``crypto.sealing._seal_with_ephemeral`` for purpose "wk" (v2 §5.3)."""
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), to))
    k = HKDF(hashes.SHA256(), 32, eph_pub + to, f"orch/v2/seal|wk|{hub_id}".encode()).derive(ss)
    aad = b"orch/v2/seal-aad|" + bytes([S.SUITE_ID, 2]) + b"wk" + bytes.fromhex(ws) + epoch.to_bytes(4, "big")
    return eph_pub + AESGCM(k).encrypt(bytes(12), wk, aad)


@pytest.fixture
def seals(tmp_path):
    d = tmp_path / "s"
    d.mkdir(mode=0o700)
    w = WkSeals(
        str(d / "wk-seal.db"),
        hub_id=G.HUB_ID,
        hxk_pub=HXK_PUB,
        exchange=lambda p: HXK_PRIV.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p)
        ),
    )
    yield w
    w.close()


@pytest.fixture
def fhub(make_hub, seals):
    return make_hub(wk_seals=seals)


def full_link(h, **kw):
    c = h.open(first=True)
    c.request("join", h.join_body(mode="full", **kw))
    c.mode = "full"
    return c


def seal_req(c, epoch, wrap):
    return c.request("wk_seal", {"epoch": epoch, "wrap": b64u(wrap)})


def test_accepted_over_real_link_and_inspect_counts(fhub, seals):
    c = full_link(fhub)
    wk = secrets.token_bytes(32)
    wrap = core_seal(wk, epoch=3)
    assert len(wrap) == 113
    assert seal_req(c, 3, wrap) == {}
    assert seals.epochs(G.WS_A) == [3]
    # the SEALED wrap is stored, no plaintext key anywhere in the file
    raw = pathlib.Path(seals._db.execute("PRAGMA database_list").fetchone()[2]).read_bytes()
    assert wk not in raw and wrap in raw


def test_selective_workspace_gets_wrong_mode(fhub, seals):
    c = fhub.open(first=True)
    c.request("join", fhub.join_body())
    c.send_raw(c.build("wk_seal", {"epoch": 1, "wrap": b64u(core_seal(b"k" * 32))}))
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == "wrong_mode"
    assert seals.epochs(G.WS_A) == []


@pytest.mark.parametrize(
    "kw",
    [
        {"hub_id": "11" * 16},  # sealed for another hub id
        {"ws": G.WS_B},  # bound to another workspace
        {"epoch": 2},  # bound to another epoch than the one stated (stated 1)
    ],
)
def test_wrong_binding_is_bad_wrap(fhub, seals, kw):
    c = full_link(fhub)
    with pytest.raises(LinkError) as e:
        seal_req(c, 1, core_seal(b"k" * 32, **kw))
    assert e.value.code == "bad_wrap"
    assert seals.epochs(G.WS_A) == []


def test_wrap_to_other_key_and_garbage_are_bad_wrap(fhub):
    c = full_link(fhub)
    other = S.public_bytes(ec.generate_private_key(ec.SECP256R1()))
    for bad in (core_seal(b"k" * 32, to=other), b"x" * 113, b"x" * 10, bytes(113)):
        with pytest.raises(LinkError) as e:
            seal_req(c, 1, bad)
        assert e.value.code == "bad_wrap"


@pytest.mark.parametrize("epoch", [0, -1, 2**32, True, "1"])
def test_bad_epoch_values_malformed(fhub, epoch):
    c = full_link(fhub)
    with pytest.raises(LinkError) as e:
        c.request("wk_seal", {"epoch": epoch, "wrap": b64u(core_seal(b"k" * 32))})
    assert e.value.code == "malformed"


def test_replay_idempotent_then_conflict_then_lower_refused(fhub, seals):
    c = full_link(fhub)
    w2 = core_seal(b"a" * 32, epoch=2)
    assert seal_req(c, 2, w2) == {}
    assert seal_req(c, 2, w2) == {}  # identical bytes: idempotent
    with pytest.raises(LinkError) as e:
        seal_req(c, 2, core_seal(b"a" * 32, epoch=2))  # same epoch, other bytes
    assert e.value.code == "bad_wrap"
    with pytest.raises(LinkError) as e:
        seal_req(c, 1, core_seal(b"b" * 32, epoch=1))  # lower
    assert e.value.code == "bad_epoch"
    assert seal_req(c, 3, core_seal(b"c" * 32, epoch=3)) == {}
    assert seals.epochs(G.WS_A) == [2, 3]


def test_after_unbind_refused_and_epochs_retired(fhub, seals):
    c = full_link(fhub)
    assert seal_req(c, 1, core_seal(b"a" * 32, epoch=1)) == {}
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert seals.epochs(G.WS_A) == []  # retired: kept in the file, never current
    with pytest.raises(LinkError) as e:
        seal_req(c, 2, core_seal(b"a" * 32, epoch=2))
    assert e.value.code == "not_bound"
    rows = seals._db.execute("SELECT epoch, retired FROM wk_seal").fetchall()
    assert rows == [(1, 1)]


def test_switch_to_selective_retires(fhub, seals):
    c = full_link(fhub)
    assert seal_req(c, 1, core_seal(b"a" * 32, epoch=1)) == {}
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="selective")}) == {}
    assert seals.epochs(G.WS_A) == []


def test_rejoin_full_may_reseal_same_epoch(fhub, seals):
    c = full_link(fhub)
    assert seal_req(c, 1, core_seal(b"a" * 32, epoch=1)) == {}
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="selective")}) == {}
    c.mode = "selective"
    assert c.request("binding_put", {"object": G.binding(seq=3, mode="full")}) == {}
    c.mode = "full"
    assert seal_req(c, 1, core_seal(b"a" * 32, epoch=1)) == {}  # fresh wrap, same epoch, new binding
    assert seals.epochs(G.WS_A) == [1]


def test_no_store_configured_is_unavailable(make_hub):
    h = make_hub()
    c = full_link(h)
    with pytest.raises(LinkError) as e:
        seal_req(c, 1, core_seal(b"a" * 32))
    assert e.value.code == "unavailable"


def test_file_is_private(seals):
    path = seals._db.execute("PRAGMA database_list").fetchone()[2]
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


def test_inspect_lists_epochs_without_key_bytes(tmp_path):
    """serve-shaped state dir: registry.db + wk-seal.db next to the hub keys."""
    state = tmp_path / "hub"
    HubKeys.init(state)
    keys = HubKeys.open(state)
    from orch_hub.link import Registry

    reg = Registry(str(state / "registry.db"))
    ws = WkSeals(str(state / "wk-seal.db"), hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange)
    reg.commit_join(
        G.WS_A,
        G.P_ALICE,
        G.K.b("wsk_a"),
        b64u(G.K.pub["pk_alice"]),
        G.v2_card(),
        {"binding_seq": 1, "status": "bound", "binding": {"mode": "full"}},
    )
    wk = secrets.token_bytes(32)
    wrap = core_seal(wk, hub_id=keys.hub_id, epoch=4, to=keys.hxk_pub)
    ws.accept(G.WS_A, 4, wrap)
    ws.close()
    reg.close()
    out = hub_inspect(state)["workspaces"][G.WS_A]
    assert out["wk_epochs"] == [4]
    assert b64u(wrap) not in repr(out) and wk.hex() not in repr(out)
    con = sqlite3.connect(state / "wk-seal.db")
    assert con.execute("SELECT count(*) FROM wk_seal").fetchone()[0] == 1


def test_real_core_wrap_is_accepted(fhub, seals):
    """Cross-package check: the core package of this repository; its own ``memberlist.seal_wk`` builds the wrap."""
    import subprocess
    import sys

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core")
    src = os.path.join(root, "src")
    code = (
        "import sys,base64;sys.path.insert(0,sys.argv[1]);from orch.identity import memberlist as M;"
        "w=M.seal_wk(bytes.fromhex(sys.argv[2]),sys.argv[3],sys.argv[4],int(sys.argv[5]),b'k'*32);"
        "print(base64.urlsafe_b64encode(w).decode().rstrip('='))"
    )
    py = os.path.join(root, ".venv/bin/python")
    if not os.path.exists(py):
        py = sys.executable
    out = subprocess.run(
        [py, "-c", code, src, HXK_PUB.hex(), G.HUB_ID, G.WS_A, "5"], check=False, capture_output=True, text=True
    )
    if out.returncode != 0:
        pytest.skip(f"core import failed: {out.stderr[-200:]}")
    from orch_hub.contract.canon import unb64u

    c = full_link(fhub)
    assert seal_req(c, 5, unb64u(out.stdout.strip())) == {}
    assert seals.epochs(G.WS_A) == [5]
