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

    from ..wsproto import corpus

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


# --- review of #512 (Opus I4, I1) ---------------------------------------------------------------------------------


def test_init_removes_leftover_temporary_key_dirs_only(tmp_path, prov):
    """An interrupted init leaves ``.keys.tmp<pid>.<8 hex>`` (encrypted, never valid): the next init removes exactly
    those, owned by this user and not through a symbolic link; nothing else."""
    root = tmp_path / "person" / "primary"
    root.mkdir(parents=True, mode=0o700)
    (tmp_path / "person").chmod(0o700)
    left = root / ".keys.tmp999999.0a1b2c3d"  # a pid no process has
    left.mkdir()
    (left / "primary-sig.key.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    link = root / ".keys.tmp4243.deadbeef"
    link.symlink_to(outside)
    others = [root / ".keys.tmpx", root / ".keys.tmp1.0A1B2C3D", root / "keys.tmp1.0a1b2c3d", root / "notes"]
    for o in others:
        o.mkdir()
    afile = root / ".keys.tmp7.00000000"
    afile.write_text("not a dir")
    primary.init(tmp_path)
    assert not left.exists()
    assert link.is_symlink() and (outside / "keep").read_text() == "x"
    assert all(o.is_dir() for o in others) and afile.read_text() == "not a dir"
    assert primary.keys_dir(tmp_path).is_dir()


def test_a_damaged_kx_file_is_not_a_wrong_passphrase(tmp_path, prov):
    """Opus I1: the signing key opens with the passphrase but the kx key does not: the files are damaged."""
    info = primary.init(tmp_path)
    keys = primary.keys_dir(tmp_path)
    other = PassphraseBackend(tmp_path / "other", _passphrase_provider=lambda r: "Kp4#vR9!wZx2-Lm7qT", _kdf=FAST,
                              _min_n=2**10)  # fmt: skip
    other._create_key(
        "primary-kx", secret=None, role="kx", passphrase=None, purposes=("cert-request",), recipient=info.device_id
    )
    (keys / "primary-kx.key.json").unlink()
    (tmp_path / "other" / "primary-kx.key.json").rename(keys / "primary-kx.key.json")
    with pytest.raises(CustodyError, match="damaged") as e:
        with primary.unlocked(tmp_path, action="x"):
            pytest.fail("unlocked a damaged pair")
    assert not isinstance(e.value, WrongPassphrase)
    prov.phrase = "the wrong phrase"
    with pytest.raises(WrongPassphrase):  # a wrong passphrase is still that
        with primary.unlocked(tmp_path, action="x"):
            pass


# --- review of #512, round 2 (Opus delta) ---------------------------------------------------------------------------


def _leftover_root(tmp_path):
    root = tmp_path / "person" / "primary"
    root.mkdir(parents=True, mode=0o700)
    (tmp_path / "person").chmod(0o700)
    return root


def test_a_leftover_of_a_live_process_survives(tmp_path, prov):
    root = _leftover_root(tmp_path)
    live = root / f".keys.tmp{os.getppid()}.0a1b2c3d"  # the test runner's parent: alive, not us
    live.mkdir()
    dead = root / ".keys.tmp999999.0a1b2c3d"
    dead.mkdir()
    primary.init(tmp_path)
    assert live.is_dir() and not dead.exists()


def test_a_concurrent_init_loses_with_key_exists(tmp_path, monkeypatch, prov):
    """A second init that starts while the first is creating keys leaves the first's temporary directory alone; the
    first then loses the rename with KeyExists, never a raw FileNotFoundError."""
    real_provider_call = prov.__call__
    done = {}

    def second_init_inside_the_first(req):
        if "second" not in done:
            done["second"] = True
            monkeypatch.setattr(primary, "_pid", lambda: os.getpid() + 1)  # another process, as the sweep sees it
            done["info"] = primary.init(tmp_path)
            monkeypatch.setattr(primary, "_pid", os.getpid)
        return real_provider_call(req)

    monkeypatch.setattr(
        primary,
        "open_backend",
        lambda d: PassphraseBackend(d, _passphrase_provider=second_init_inside_the_first, _kdf=FAST, _min_n=2**10),
    )
    with pytest.raises(KeyExists):
        primary.init(tmp_path)
    assert primary.load(tmp_path) == done["info"]
    assert [p.name for p in (tmp_path / "person" / "primary").iterdir()] == ["keys"]


def test_the_sweep_tolerates_a_vanished_entry_and_wraps_other_os_errors(tmp_path, monkeypatch, prov):
    root = _leftover_root(tmp_path)
    (root / ".keys.tmp999999.0a1b2c3d").mkdir()
    real = primary.shutil.rmtree

    def vanished(path, *a, **k):
        if ".keys.tmp999999." in str(path):
            real(path)
            raise FileNotFoundError(path)
        return real(path, *a, **k)

    monkeypatch.setattr(primary.shutil, "rmtree", vanished)
    primary.init(tmp_path)  # another init removed it first: fine
    other = tmp_path / "second"
    root2 = _leftover_root(other)
    (root2 / ".keys.tmp999999.0a1b2c3d").mkdir()

    def busy(path, *a, **k):
        if ".keys.tmp999999." in str(path):
            raise OSError(16, "busy")
        return real(path, *a, **k)

    monkeypatch.setattr(primary.shutil, "rmtree", busy)
    with pytest.raises(CustodyError, match="leftover"):
        primary.init(other)
    assert not primary.keys_dir(other).exists()


def test_only_create_and_create_primary_call_create_key():
    """Production code makes keys through ``create`` (refuses primary and kx) or ``create_primary``; nothing else
    reaches the private ``_create_key`` (tests excluded)."""
    import ast

    root = Path(primary.__file__).parents[1]
    callers = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute) and node.attr == "_create_key") or (
                    isinstance(node, ast.Name) and node.id == "_create_key"
                ):
                    callers.add(f"{rel}:{fn.name}")
        for node in tree.body:  # module level: never
            for sub in ast.walk(node):
                if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) and (
                    (isinstance(sub, ast.Attribute) and sub.attr == "_create_key")
                    or (isinstance(sub, ast.Name) and sub.id == "_create_key")
                ):
                    callers.add(f"{rel}:<module>")
        if "_create_key" in path.read_text(encoding="utf-8") and rel != "custody/passphrase.py":
            callers.add(f"{rel}:<text>")
    assert callers == {"custody/passphrase.py:create", "custody/passphrase.py:create_primary"}
