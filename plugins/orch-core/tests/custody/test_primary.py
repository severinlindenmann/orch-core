"""The primary key pair's custody (orch-v2-host 11A.15.1, 11A.15.10 item 9): ``orch.custody.primary``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from orch import crypto
from orch.crypto.labels import L
from orch.custody import CustodyError, KdfParams, KeyExists, PassphraseBackend, WrongPassphrase, primary
from orch.custody.convenience import ConvenienceBackend, FakeStore

FAST = KdfParams(n=2**10)
PHRASE = "Zq7!mPx2-vL9#rTb4w"
RID = bytes.fromhex("ab" * 16)


class Provider:
    def __init__(self, phrase: str = PHRASE) -> None:
        self.phrase = phrase
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        return self.phrase


@pytest.fixture
def prov(monkeypatch):
    p = Provider()
    monkeypatch.setattr(
        primary, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=p, _kdf=FAST, _min_n=2**10)
    )
    return p


def _seal_request(info, pt=b'{"inner":1}', rid=RID, purpose="cert-request", epoch=0):
    return crypto.seal(info.dk_kx_pub, info.device_id, purpose, rid, epoch, pt)


def test_init_makes_the_pair_under_one_passphrase_prompt(tmp_path, prov):
    info = primary.init(tmp_path)
    assert [r.kind for r in prov.requests] == ["create"]
    assert info.device_id == crypto.device_id(info.dk_sig_pub)
    assert info.device == "d_" + info.device_id.hex()
    assert info.purposes == ("cert-request",) and info.custody == "passphrase"
    keys = primary.keys_dir(tmp_path)
    assert keys == tmp_path / "person" / "primary" / "keys"
    assert sorted(p.name for p in keys.iterdir()) == ["primary-kx.key.json", "primary-sig.key.json"]
    kx = json.loads((keys / "primary-kx.key.json").read_text())
    assert kx["role"] == "kx" and kx["purposes"] == ["cert-request"] and kx["recipient"] == info.device_id.hex()
    assert json.loads((keys / "primary-sig.key.json").read_text())["role"] == "primary"
    if os.name != "nt":
        assert (keys.stat().st_mode & 0o777) == 0o700
        assert (keys.parent.stat().st_mode & 0o777) == 0o700


def test_init_refuses_a_second_primary_and_changes_nothing(tmp_path, prov):
    info = primary.init(tmp_path)
    with pytest.raises(KeyExists):
        primary.init(tmp_path)
    assert primary.load(tmp_path) == info


def test_a_failed_init_leaves_no_half_pair(tmp_path, monkeypatch, prov):
    real = PassphraseBackend._create_key

    def fail_kx(self, key_id, **kw):
        if kw.get("role") == "kx":
            raise CustodyError("disk full")
        return real(self, key_id, **kw)

    monkeypatch.setattr(PassphraseBackend, "_create_key", fail_kx)
    with pytest.raises(CustodyError):
        primary.init(tmp_path)
    assert not primary.keys_dir(tmp_path).exists()
    assert not [p for p in (tmp_path / "person" / "primary").iterdir()]
    assert primary.status(tmp_path) == {"present": False, "custody": None}


def test_status_is_public_and_never_prompts(tmp_path, prov):
    assert primary.status(tmp_path) == {"present": False, "custody": None}
    info = primary.init(tmp_path)
    n = len(prov.requests)
    st = primary.status(tmp_path)
    assert len(prov.requests) == n
    assert st == {
        "present": True,
        "custody": "passphrase",
        "device": info.device,
        "dk_sig_pub": crypto.b64u(info.dk_sig_pub),
        "dk_kx_pub": crypto.b64u(info.dk_kx_pub),
        "purposes": ["cert-request"],
        "certificate": None,
        "relay": None,
    }
    text = json.dumps(st)
    for f in primary.keys_dir(tmp_path).iterdir():
        doc = json.loads(f.read_text())
        assert doc["ct"] not in text and doc["kdf"]["salt"] not in text and doc["nonce"] not in text


def test_a_mismatched_pair_is_refused(tmp_path, prov):
    primary.init(tmp_path)
    other = tmp_path / "other"
    primary.init(other)
    keys = primary.keys_dir(tmp_path)
    (keys / "primary-kx.key.json").unlink()
    (primary.keys_dir(other) / "primary-kx.key.json").rename(keys / "primary-kx.key.json")
    with pytest.raises(CustodyError):
        primary.load(tmp_path)
    with pytest.raises(CustodyError):
        with primary.unlocked(tmp_path, action="pair"):
            pass


def test_one_unlock_serves_the_whole_command(tmp_path, prov):
    info = primary.init(tmp_path)
    n = len(prov.requests)
    login = L["sig_relay_auth"] + b"\x02challenge"
    with primary.unlocked(tmp_path, action="pair a phone") as keys:
        for i in range(3):
            assert crypto.verify(info.dk_sig_pub, keys.sign(login + bytes([i])), login + bytes([i]))
        opener = primary.cert_request_unseal(keys)
        for i in range(3):
            assert opener(RID, _seal_request(info, pt=bytes([i]))) == bytes([i])
    assert [r.kind for r in prov.requests[n:]] == ["primary"]
    shown = primary.render_unlock(prov.requests[n])
    assert "pair a phone" in shown and info.device in shown and "signs nothing else" in shown


def test_the_unlocked_pair_signs_relay_logins_only_and_opens_cert_requests_only(tmp_path, prov):
    info = primary.init(tmp_path)
    with primary.unlocked(tmp_path, action="x") as keys:
        for label in ("sig_device_cert", "sig_cert_challenge", "sig_decision", "sig_bridge", "sig_ws_cosign"):
            with pytest.raises(CustodyError):
                keys.sign(L[label] + b"{}")
        for purpose in ("wk", "card", "ws-envelope"):
            with pytest.raises(CustodyError):
                keys.unseal(purpose, RID, 0, b"", _seal_request(info, purpose=purpose))
        with pytest.raises(CustodyError):
            keys.unseal("cert-request", RID, 1, b"", _seal_request(info, epoch=1))
    with pytest.raises(CustodyError):  # locked again after the block
        keys.sign(L["sig_relay_auth"] + b"x")
    with pytest.raises(CustodyError):
        primary.cert_request_unseal(keys)(RID, _seal_request(info))


def test_the_adapter_keeps_open_cert_requests_contract(tmp_path, prov):
    """``unseal(object_id, sealed)``: purpose ``cert-request``, epoch 0, the object id as given; a blob that does not
    open raises InvalidTag (open_cert_request turns that into ``malformed``)."""
    info = primary.init(tmp_path)
    blob = _seal_request(info)
    with primary.unlocked(tmp_path, action="x") as keys:
        opener = primary.cert_request_unseal(keys)
        assert opener(RID, blob) == b'{"inner":1}'
        with pytest.raises(InvalidTag):
            opener(bytes(16), blob)
        with pytest.raises(InvalidTag):
            opener(RID, blob[:-1] + bytes([blob[-1] ^ 1]))
        with pytest.raises(InvalidTag):  # sealed to another recipient
            opener(RID, crypto.seal(info.dk_kx_pub, bytes(16), "cert-request", RID, 0, b"x"))
    per_call = primary.backend_cert_request_unseal(tmp_path)
    assert per_call(RID, blob) == b'{"inner":1}'


def test_a_wrong_passphrase_opens_nothing(tmp_path, prov):
    primary.init(tmp_path)
    prov.phrase = "the wrong phrase"
    with pytest.raises(WrongPassphrase):
        with primary.unlocked(tmp_path, action="x"):
            pytest.fail("unlocked with a wrong passphrase")


def test_no_primary_is_key_not_found(tmp_path, prov):
    from orch.custody import KeyNotFound

    with pytest.raises(KeyNotFound):
        primary.load(tmp_path)
    with pytest.raises(KeyNotFound):
        with primary.unlocked(tmp_path, action="x"):
            pass


# --- convenience custody is refused for the primary (11A.15.6, owner rule) -------------------------------------------


def _convenience(_d: Path):
    key = crypto.generate_private_key()
    pub = crypto.public_bytes(key)
    from orch.identity import device_ref

    return ConvenienceBackend(FakeStore(), "705d40abbb8c1c90354a1acaa94c935c", device_ref(pub), pub)


class _LookAlike(PassphraseBackend):
    auth = "convenience"


@pytest.mark.parametrize("make", [_convenience, lambda d: _LookAlike(d, _kdf=FAST, _min_n=2**10)])
def test_primary_refuses_any_custody_but_passphrase(tmp_path, monkeypatch, prov, make):
    primary.init(tmp_path / "made")
    monkeypatch.setattr(primary, "open_backend", make)
    for call in (
        lambda: primary.init(tmp_path / "fresh"),
        lambda: primary.load(tmp_path / "made"),
        lambda: primary.unlocked(tmp_path / "made", action="x").__enter__(),
        lambda: primary.backend_cert_request_unseal(tmp_path / "made"),
    ):
        with pytest.raises(CustodyError) as e:
            call()
        assert e.value.code == "sign.convenience_unavailable" and str(e.value).startswith("primary:")
    assert not primary.keys_dir(tmp_path / "fresh").exists()


def test_custody_convenience_refuses_primary_role(tmp_path, prov, monkeypatch):
    """``orch keys custody convenience`` exports only a device key: the primary's keys, even copied to a workspace's
    device key path, never move into convenience custody."""
    from orch.custody import select
    from orch.ops import human

    monkeypatch.setattr(
        human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=prov, _kdf=FAST, _min_n=2**10)
    )
    primary.init(tmp_path)
    ws = "705d40abbb8c1c90354a1acaa94c935c"
    dk_dir = human.keys_dir(tmp_path, ws)
    dk_dir.mkdir(parents=True)
    for name in ("primary-sig", "primary-kx"):
        doc = json.loads((primary.keys_dir(tmp_path) / f"{name}.key.json").read_text())
        doc["key_id"] = "dk"
        path = dk_dir / human.KEY_FILE
        path.write_text(json.dumps(doc))
        path.chmod(0o600)
        n = len(prov.requests)
        with pytest.raises(CustodyError, match="only a device key changes custody"):
            select.convert_to_convenience(tmp_path, ws, store=FakeStore())
        assert len(prov.requests) == n  # refused before any prompt
        path.unlink()


def test_the_primary_module_reads_no_environment():
    """Environment variables may only narrow (11A.15.11 N8a); the primary selects no backend from one."""
    src = Path(primary.__file__).read_text()
    assert "os.environ" not in src and "getenv" not in src and "import os.environ" not in src


def test_only_the_primary_module_unlocks_the_primary_pair():
    root = Path(primary.__file__).parents[1]
    users = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py") if ".primary_unlocked(" in p.read_text())
    assert users == ["custody/primary.py"]


# --- with the H12c-b builders: a real cert request opened by the custody primary ----------------------------------


def _request_to(info):
    """A §8.4 request from the corpus host (WSK ``wsk_a``) to this custody primary, checked as the primary checks it."""
    from orch import canon
    from orch.identity import certreq, wscard

    from ..relay import corpus

    s2 = corpus.S2
    cr, keys = s2["cert_request"], s2["keys"]
    inner = cr["inner"]
    o = certreq.cert_request_o(
        request_id=RID.hex(),
        workspace_id=cr["signed"]["o"]["workspace_id"],
        person_id=cr["signed"]["o"]["person_id"],
        primary_device_id=info.device_id.hex(),
        primary_kx_pub=info.dk_kx_pub,
        offer_id=inner["offer_id"],
        attempt=inner["attempt"],
        dk_sig_pub=crypto.unb64u(inner["dk_sig_pub"]),
        dk_kx_pub=crypto.unb64u(inner["dk_kx_pub"]),
        label=inner["label"],
        scopes_max=inner["scopes_max"],
        created_ms=cr["signed"]["o"]["created_ms"],
    )
    seed = int.from_bytes(bytes.fromhex(keys["wsk_a.sig"]["seed"]), "big")
    wsk = crypto.private_key_from_scalar(seed % (crypto.P256_N - 1) + 1)
    signed = {"o": o, "sig": crypto.b64u(crypto.sign(wsk, L["sig_cert_request"] + canon.dumps(o)))}
    card = wscard.verify_card(cr["cases"][0]["card"], corpus.pub("person_alice.sig"))
    req = certreq.check_cert_request(
        signed,
        card,
        me_person_id=s2["ids"]["person_alice"],
        me_device_id=info.device_id.hex(),
        journal=certreq.CertJournal(),
        now_ms=o["created_ms"],
    )
    return req, inner


def test_the_custody_primary_opens_a_real_cert_request(tmp_path, prov):
    from orch.identity import certreq

    info = primary.init(tmp_path)
    req, inner = _request_to(info)
    with primary.unlocked(tmp_path, action="pair") as keys:
        got = certreq.open_cert_request(req, primary.cert_request_unseal(keys))
    assert got.as_dict() == inner and got.request_id == req.request_id
    assert certreq.open_cert_request(req, primary.backend_cert_request_unseal(tmp_path)).as_dict() == inner


def test_a_custody_refusal_is_not_a_malformed_request(tmp_path, prov):
    from orch.identity import certreq

    info = primary.init(tmp_path)
    req, _ = _request_to(info)
    with primary.unlocked(tmp_path, action="pair") as keys:
        opener = primary.cert_request_unseal(keys)
    with pytest.raises(CustodyError):  # locked again: a custody error, not Refused("malformed")
        certreq.open_cert_request(req, opener)
    prov.phrase = "the wrong phrase"
    with pytest.raises(WrongPassphrase):
        certreq.open_cert_request(req, primary.backend_cert_request_unseal(tmp_path))
