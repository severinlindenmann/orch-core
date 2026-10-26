"""The hub key store and issuer (V3-D2, docs/protocol.md §2-§6).

Layout of the state dir (all directories 0700, all files 0600, owned by the running user)::

    meta.json          {hub_id, op_seq, card_seq, relay_url, tls_spki_sha256}      committed last
    hk_root.key        HK_root scalar (SHORTCUT #909: online)
    hxk.key            HXK, the hub's key-agreement key
    uk/<user_id>.key   one custodial UK per user
    op/<op_seq>/       everything that depends on one op cert: key, cert.json, card.json, uk/<user>.json, ch/<id>.json
    revoked/<cert_id>.json   root-signed hub.op_revoked, only ever added

A rotation builds ``op/<N+1>/`` completely and then replaces ``meta.json`` (atomic): a crash before that leaves the old
generation serving, a crash after it leaves the new one, never a mix. Private keys leave this module only as the typed
signers of ``orch_hub.contract.keystore`` (and :meth:`HubKeys.hxk_exchange`); they are never logged or printed.
Every issued object is verified with the contract's own verifier before it is stored or returned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from ..contract import hub as H
from ..contract import keystore as KS
from ..contract import suite as S
from ..contract.canon import CanonError, b64u, cj, unb64u
from ..tlsid import TlsIdentity
from .fsutil import StateError, check_dir, make_dir, read_file, write_atomic

_CH_FILE = re.compile(r"[0-9a-f]{32}\.json")
_UK_FILE = re.compile(r"[0-9a-f]{32}\.json")
_GEN_DIR = re.compile(r"[0-9]{1,15}")

DEV = True  # V3: every hub object is dev-only (protocol §2.4)
DAY_MS = 86_400_000
OP_LIFETIME_MS = 366 * DAY_MS  # matches the H0 vectors (issued NOW-1d, expires NOW+365d)
UK_LIFETIME_MS = 31 * DAY_MS
CHANNEL_LIFETIME_MS = 31 * DAY_MS
TLS_KEY_FILE = "tls.key"  # hex P-256 scalar like the other keys: 0600, owned by the hub user
PLACEHOLDER_TLS = b64u(S.sha256(b"orch-hub dev: no TLS key pinned yet"))  # never matches a real key
_HEX16 = re.compile(r"[0-9a-f]{32}")
ENV_STATE = "ORCH_HUB_STATE"

KeyGen = Callable[[str], ec.EllipticCurvePrivateKey]


class KeysError(Exception):
    """A refusal with a stable ``code``. Never carries key material."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def default_state_dir() -> Path:
    v = os.environ.get(ENV_STATE)
    if not v:
        raise KeysError("no_state_dir", f"set {ENV_STATE} or pass --state")
    return Path(v)


def _gen(purpose: str) -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _new_id() -> str:
    return os.urandom(16).hex()


def _hex16(x: object, what: str) -> str:
    if type(x) is not str or not _HEX16.fullmatch(x):
        raise KeysError("malformed", f"{what} must be 32 lower-case hex characters")
    return x


def _key_bytes(k: ec.EllipticCurvePrivateKey) -> bytes:
    return (k.private_numbers().private_value.to_bytes(32, "big").hex() + "\n").encode("ascii")


def _load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    raw = read_file(path).decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise StateError("bad_key_file", path.name)  # never echo the content
    n = int(raw, 16)
    if not 0 < n < S.P256_N:
        raise StateError("bad_key_file", path.name)
    return ec.derive_private_key(n, ec.SECP256R1())


def _json_bytes(o: Any) -> bytes:
    return cj(o) + b"\n"


def _load_json(path: Path) -> Any:
    try:
        return H.parse(read_file(path))
    except H.Refused:
        raise StateError("bad_file", path.name) from None


class HubKeys:
    """An opened hub key store. Use :meth:`init` once, then :meth:`open`."""

    def __init__(
        self,
        state: Path,
        *,
        now_ms: Callable[[], int] | None = None,
        new_id: Callable[[], str] | None = None,
        keygen: KeyGen | None = None,
        signer_classes: Mapping[str, type] | None = None,
    ) -> None:
        self.state = Path(state)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._new_id = new_id or _new_id
        self._keygen = keygen or _gen
        sc = {"root": KS.RootSigner, "op": KS.OpSigner, "uk": KS.UkSigner}
        sc.update(signer_classes or {})
        self._sc = sc
        self._root: KS.RootSigner | None = None
        self._op: KS.OpSigner | None = None
        self._hxk: ec.EllipticCurvePrivateKey | None = None
        self._meta: dict[str, Any] = {}
        self.dev = DEV  # replaced by init (--real) or by meta.json when opened

    # --- creation / opening ---------------------------------------------------------------------------------------

    @classmethod
    def init(
        cls,
        state: Path,
        *,
        relay_url: str | None = None,
        tls_spki_sha256: str | None = None,
        dev: bool | None = None,
        **kw: Any,
    ) -> HubKeys:
        """Create a new hub identity: HK_root, the first HK_op (op_seq 1), HXK, and the first card (card_seq 1)."""
        self = cls(state, **kw)
        self.dev = DEV if dev is None else dev
        try:
            if self.state.exists() and any(self.state.iterdir()):
                raise KeysError("already_initialised", "the state dir is not empty")
            self.state.parent.mkdir(parents=True, exist_ok=True)
            make_dir(self.state)
            for sub in ("uk", "op", "revoked"):
                make_dir(self.state / sub)
            root = self._keygen("hk_root")
            hxk = self._keygen("hxk")
            write_atomic(self.state / "hk_root.key", _key_bytes(root))
            write_atomic(self.state / "hxk.key", _key_bytes(hxk))
            if tls_spki_sha256 is None:  # D12b G4: the TLS key is made here, once, and the first card pins it
                tls_key = _gen("tls")
                write_atomic(self.state / TLS_KEY_FILE, _key_bytes(tls_key))
                tls_spki_sha256 = TlsIdentity.from_key(tls_key).spki_sha256
            hub_id = H.hub_id(S.public_bytes(root))
            self._meta = {
                "v": 1,
                "hub_id": hub_id,
                "op_seq": 0,
                "card_seq": 0,
                "relay_url": relay_url,
                "tls_spki_sha256": tls_spki_sha256 or PLACEHOLDER_TLS,
                "dev": self.dev,
            }
            self._root = self._sc["root"](root)
            self._hxk = hxk
            self._rotate(first=True)
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return self

    @classmethod
    def open(cls, state: Path, *, prune: bool = True, **kw: Any) -> HubKeys:
        """Open an existing store. Fails closed on any wrong mode, owner, type, missing or inconsistent file.
        ``prune=False`` never removes anything (read-only callers such as ``doctor``: a stray ``op/<n>`` may be a
        rotation in progress)."""
        self = cls(state, **kw)
        try:
            check_dir(self.state)
            for sub in ("uk", "op", "revoked"):
                check_dir(self.state / sub)
            m = _load_json(self.state / "meta.json")
            if (
                type(m) is not dict
                or set(m) != {"v", "hub_id", "op_seq", "card_seq", "relay_url", "tls_spki_sha256", "dev"}
                or type(m["dev"]) is not bool
                or m["v"] != 1
                or type(m["op_seq"]) is not int
                or m["op_seq"] < 1
                or type(m["card_seq"]) is not int
                or m["card_seq"] < 1
            ):
                raise KeysError("bad_state", "meta.json")
            self._meta = m
            self.dev = m["dev"]
            root = _load_key(self.state / "hk_root.key")
            if H.hub_id(S.public_bytes(root)) != m["hub_id"]:
                raise KeysError("bad_state", "hk_root.key does not match the hub id")
            self._root = self._sc["root"](root)
            self._hxk = _load_key(self.state / "hxk.key")
            gen = self._gen_dir()
            check_dir(gen)
            op_key = _load_key(gen / "key")
            cert = _load_json(gen / "cert.json")
            if (
                type(cert) is not dict
                or type(cert.get("o")) is not dict
                or cert["o"].get("hk_op_pub") != b64u(S.public_bytes(op_key))
                or cert["o"].get("op_seq") != m["op_seq"]
            ):
                raise KeysError("bad_state", "the op key does not match its certificate")
            try:
                H.verify_op_cert(
                    cert,
                    S.public_bytes(root),
                    now_ms=self._now(),
                    dev_mode=self.dev,
                    revoked_op_certs=self.revoked_op_certs(),
                )
            except H.Refused as r:
                if r.code not in ("cert_expired", "revoked"):  # those are handled at use; rotate-op must still work
                    raise KeysError("bad_state", f"op certificate: {r.code}") from None
            self._op = self._sc["op"](op_key)
            if prune:
                self._prune_gens()
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return self

    # --- read side ------------------------------------------------------------------------------------------------

    def _gen_dir(self, seq: int | None = None) -> Path:
        return self.state / "op" / str(self._meta["op_seq"] if seq is None else seq)

    @property
    def hub_id(self) -> str:
        return self._meta["hub_id"]

    @property
    def hk_root_pub(self) -> bytes:
        assert self._root is not None
        return self._root.public

    @property
    def hxk_pub(self) -> bytes:
        assert self._hxk is not None
        return S.public_bytes(self._hxk)

    @property
    def op_seq(self) -> int:
        return self._meta["op_seq"]

    @property
    def card_seq(self) -> int:
        return self._meta["card_seq"]

    def op_cert(self) -> dict[str, Any]:
        return self._read_obj(self._gen_dir() / "cert.json")

    def card(self) -> dict[str, Any]:
        return self._read_obj(self._gen_dir() / "card.json")

    def tls_identity(self) -> TlsIdentity | None:
        """The persistent TLS identity of this hub, or None for a store made before D12b (``rotate_tls`` adds one)."""
        path = self.state / TLS_KEY_FILE
        try:
            if not os.path.lexists(path):
                return None
            return TlsIdentity.from_key(_load_key(path))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None

    def rotate_tls(self) -> dict[str, Any]:
        """New TLS key (written first, 0600), then the card re-issued with its pin (card_seq + 1). A crash between the
        two leaves the key and the card disagreeing; ``serve`` refuses that (``tls_pin_mismatch``): run this again."""
        self._require_op()
        key = _gen("tls")
        try:
            write_atomic(self.state / TLS_KEY_FILE, _key_bytes(key))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return self.reissue_card(tls_spki_sha256=TlsIdentity.from_key(key).spki_sha256)

    def op_cert_by_id(self, cert_id: str) -> dict[str, Any] | None:
        """The root-signed ``hub.op_cert`` with this id among the generations still on disk (the audit chain serves the
        ones its records name); None if unknown."""
        if type(cert_id) is not str or not _HEX16.fullmatch(cert_id):
            return None
        for seq in range(self._meta["op_seq"], 0, -1):
            try:
                c = self._read_obj(self._gen_dir(seq) / "cert.json")
            except KeysError:
                continue
            if c.get("o", {}).get("cert_id") == cert_id:
                return c
        return None

    def _read_obj(self, path: Path) -> dict[str, Any]:
        try:
            o = _load_json(path)
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        if type(o) is not dict:
            raise KeysError("bad_state", path.name)
        return o

    def revoked_op_certs(self) -> frozenset[str]:
        out = set()
        rdir = self.state / "revoked"
        for p in rdir.iterdir():
            if p.suffix == ".json" and _HEX16.fullmatch(p.stem):
                out.add(p.stem)
        return frozenset(out)

    def revocations(self) -> list[dict[str, Any]]:
        return [self._read_obj(self.state / "revoked" / f"{c}.json") for c in sorted(self.revoked_op_certs())]

    @property
    def op_signer(self) -> KS.OpSigner:
        """The typed HK_op signer (link server: h2w messages; snapshots; publish transport). Refuses once revoked."""
        self._require_op()
        assert self._op is not None
        return self._op

    def uk_signer(self, user_id: str) -> KS.UkSigner:
        """The typed signer of one user's UK (custodial engine only). Refuses if the op cert is revoked or the user
        has no current certificate under it."""
        _hex16(user_id, "user_id")
        self._require_op()
        if not (self._gen_dir() / "uk" / f"{user_id}.json").exists():
            raise KeysError("unknown_user", "no current UK certificate for this user")
        try:
            return self._sc["uk"](_load_key(self.state / "uk" / f"{user_id}.key"))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None

    def uk_cert(self, user_id: str) -> dict[str, Any]:
        _hex16(user_id, "user_id")
        p = self._gen_dir() / "uk" / f"{user_id}.json"
        if not p.exists():
            raise KeysError("unknown_user", "no current UK certificate for this user")
        return self._read_obj(p)

    def channel_certs(self, user_id: str | None = None) -> list[dict[str, Any]]:
        out = [self._read_obj(p) for p in sorted((self._gen_dir() / "ch").iterdir()) if _CH_FILE.fullmatch(p.name)]
        return [c for c in out if user_id is None or c["o"]["user_id"] == user_id]

    def users(self) -> list[str]:
        return sorted(p.stem for p in (self._gen_dir() / "uk").iterdir() if _UK_FILE.fullmatch(p.name))

    def hxk_exchange(self, peer_pub: bytes) -> bytes:
        """ECDH with HXK (raw shared secret; the caller applies the key derivation of the wrap format)."""
        assert self._hxk is not None
        if not S.valid_public_key(peer_pub):
            raise KeysError("malformed", "peer key")
        return self._hxk.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_pub))

    def verify_card(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """The contract verifier run on one of our own objects, against the current revoked set."""
        try:
            return H.verify_card(
                card,
                now_ms=self._now(),
                dev_mode=self.dev,
                pinned_root=self.hk_root_pub,
                revoked_op_certs=self.revoked_op_certs(),
            )
        except H.Refused as r:
            raise KeysError(r.code, "own card failed verification") from None

    # --- issuing --------------------------------------------------------------------------------------------------

    def _require_op(self) -> None:
        cert = self.op_cert()
        if cert["o"]["cert_id"] in self.revoked_op_certs():
            raise KeysError("revoked", "the current op certificate is revoked; run rotate-op")
        if self._now() >= cert["o"]["expires_ms"]:
            raise KeysError("cert_expired", "the current op certificate expired; run rotate-op")

    def _held(self, card: dict[str, Any]) -> dict[str, Any]:
        return self.verify_card(card)

    def _sign_op_cert(self, op_key_pub: bytes, op_seq: int, now: int) -> dict[str, Any]:
        assert self._root is not None
        o = {
            "v": 2,
            "suite": S.SUITE_ID,
            "kind": "hub.op_cert",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "cert_id": _hex16(self._new_id(), "cert_id"),
            "op_seq": op_seq,
            "hk_op_pub": b64u(op_key_pub),
            "issued_ms": now,
            "expires_ms": now + OP_LIFETIME_MS,
            "dev": self.dev,
        }
        signed = self._root.sign_object(o)
        try:
            H.verify_op_cert(signed, self.hk_root_pub, now_ms=now, dev_mode=self.dev)
        except H.Refused as r:
            raise KeysError(r.code, "own op cert failed verification") from None
        return signed

    def _sign_card(
        self, signer: KS.OpSigner, op_cert: dict[str, Any], card_seq: int, now: int, relay_url: str | None, tls: str
    ) -> dict[str, Any]:
        o = {
            "v": 2,
            "suite": S.SUITE_ID,
            "kind": "hub.card",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "hk_root_pub": b64u(self.hk_root_pub),
            "hxk_pub": b64u(self.hxk_pub),
            "relay_url": relay_url,
            "tls_spki_sha256": tls,
            "card_seq": card_seq,
            "reader": H.HUB_READER,
            "issued_ms": now,
            "dev": self.dev,
        }
        return signer.sign_card(o, op_cert)

    def _rotate(self, *, first: bool = False) -> None:
        assert self._root is not None
        now = self._now()
        seq = self._meta["op_seq"] + 1
        gen = self._gen_dir(seq)
        if gen.exists():  # leftover of a crashed rotation: never committed (meta still names the old one)
            shutil.rmtree(gen)
        make_dir(gen)
        for sub in ("uk", "ch"):
            make_dir(gen / sub)
        op_key = self._keygen("hk_op")
        new_op = self._sc["op"](op_key)
        cert = self._sign_op_cert(new_op.public, seq, now)
        card_seq = self._meta["card_seq"] + 1
        card = self._sign_card(new_op, cert, card_seq, now, self._meta["relay_url"], self._meta["tls_spki_sha256"])
        users: list[str] = [] if first else self.users()
        old_ch = [] if first else self.channel_certs()
        write_atomic(gen / "key", _key_bytes(op_key))
        write_atomic(gen / "cert.json", _json_bytes(cert))
        write_atomic(gen / "card.json", _json_bytes(card))
        held = H.verify_card(
            card, now_ms=now, dev_mode=self.dev, pinned_root=self.hk_root_pub, revoked_op_certs=self.revoked_op_certs()
        )
        # Reissue every UK and channel certificate under the new op cert (protocol §5, one op cert at a time).
        for u in users:
            uk_pub = S.public_bytes(_load_key(self.state / "uk" / f"{u}.key"))
            c = self._sign_uk_cert(new_op, held, u, uk_pub, now)
            write_atomic(gen / "uk" / f"{u}.json", _json_bytes(c))
        for old in old_ch:
            o = old["o"]
            c = self._sign_channel_cert(new_op, held, o["user_id"], unb64u(o["key_pub"], 65), o["factor"], now)
            write_atomic(gen / "ch" / f"{o['channel_id']}.json", _json_bytes(c))
        self._meta = {**self._meta, "op_seq": seq, "card_seq": card_seq}
        write_atomic(self.state / "meta.json", _json_bytes(self._meta))  # the commit point
        self._op = new_op
        self._prune_gens()  # the old op key must not outlive its generation

    def _prune_gens(self) -> None:
        """Remove every ``op/<n>`` that is not the committed generation (old keys, uncommitted leftovers). Only plain
        directories named by digits directly under ``op/``; symlinks are never followed."""
        keep = str(self._meta["op_seq"])
        with os.scandir(self.state / "op") as it:
            for e in list(it):
                if e.name != keep and _GEN_DIR.fullmatch(e.name) and e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path)

    def rotate_op(self) -> dict[str, Any]:
        """New HK_op under the next op_seq (root-signed, L2-1), a new card (card_seq + 1), and every UK and channel
        certificate reissued. Returns the new card."""
        try:
            self._rotate()
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return self.card()

    def revoke_op(self, cert_id: str | None = None) -> dict[str, Any]:
        """Root-signed ``hub.op_revoked`` for ``cert_id`` (default: the current op cert). The revocation is only
        ever added. If it names the current op cert, issuing stops until :meth:`rotate_op`."""
        assert self._root is not None
        cert_id = _hex16(cert_id if cert_id is not None else self.op_cert()["o"]["cert_id"], "cert_id")
        o = {
            "v": 2,
            "suite": S.SUITE_ID,
            "kind": "hub.op_revoked",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "cert_id": cert_id,
            "revoked_ms": self._now(),
            "dev": self.dev,
        }
        signed = self._root.sign_object(o)
        try:
            H.verify_op_revoked(signed, self.hk_root_pub, dev_mode=self.dev)
        except H.Refused as r:
            raise KeysError(r.code, "own revocation failed verification") from None
        path = self.state / "revoked" / f"{cert_id}.json"
        if path.exists():
            return self._read_obj(path)  # a revocation is never replaced
        write_atomic(path, _json_bytes(signed))
        return signed

    def reissue_card(self, *, relay_url: str | None = ..., tls_spki_sha256: str | None = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Re-issue the card under the current op cert with card_seq + 1 (optionally with a new relay URL or TLS pin)."""
        self._require_op()
        meta = dict(self._meta)
        if relay_url is not ...:
            meta["relay_url"] = relay_url
        if tls_spki_sha256 is not None:
            meta["tls_spki_sha256"] = tls_spki_sha256
        card = self._sign_card(
            self.op_signer,
            self.op_cert(),
            meta["card_seq"] + 1,
            self._now(),
            meta["relay_url"],
            meta["tls_spki_sha256"],
        )
        self._held(card)
        meta["card_seq"] += 1
        try:
            # meta first: it reserves the card_seq. A crash between the two leaves a gap in card_seq (the old card
            # stays served), never one seq for two different cards.
            write_atomic(self.state / "meta.json", _json_bytes(meta))
            write_atomic(self._gen_dir() / "card.json", _json_bytes(card))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        self._meta = meta
        return card

    def _sign_uk_cert(
        self, op: KS.OpSigner, held: dict[str, Any], user_id: str, uk_pub: bytes, now: int
    ) -> dict[str, Any]:
        o = {
            "v": 2,
            "suite": S.SUITE_ID,
            "kind": "hub.uk_cert",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "op_cert_id": held["op_cert"]["cert_id"],
            "user_id": user_id,
            "uk_pub": b64u(uk_pub),
            "issued_ms": now,
            "expires_ms": now + UK_LIFETIME_MS,
            "dev": self.dev,
        }
        signed = op.sign_object(o)
        try:
            H.verify_uk_cert(signed, held, now_ms=now, dev_mode=self.dev, revoked_op_certs=self.revoked_op_certs())
        except H.Refused as r:
            raise KeysError(r.code, "own UK certificate failed verification") from None
        return signed

    def _sign_channel_cert(
        self, op: KS.OpSigner, held: dict[str, Any], user_id: str, key_pub: bytes, factor: str, now: int
    ) -> dict[str, Any]:
        o = {
            "v": 2,
            "suite": S.SUITE_ID,
            "kind": "hub.channel_cert",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "op_cert_id": held["op_cert"]["cert_id"],
            "channel_id": H.channel_id(key_pub),
            "user_id": user_id,
            "key_pub": b64u(key_pub),
            "factor": factor,
            "issued_ms": now,
            "expires_ms": now + CHANNEL_LIFETIME_MS,
            "dev": self.dev,
        }
        signed = op.sign_object(o)
        try:
            H.verify_channel_cert(signed, held, now_ms=now, dev_mode=self.dev, revoked_op_certs=self.revoked_op_certs())
        except H.Refused as r:
            raise KeysError(r.code, "own channel certificate failed verification") from None
        return signed

    def issue_uk(self, user_id: str | None = None) -> dict[str, Any]:
        """Create the user's UK on first use (``user_id`` None: a new random user) and issue a fresh certificate under
        the current op cert. Returns the signed ``hub.uk_cert``."""
        self._require_op()
        try:
            if user_id is None:
                user_id = _hex16(self._new_id(), "user_id")
            _hex16(user_id, "user_id")
            keyfile = self.state / "uk" / f"{user_id}.key"
            if keyfile.exists():
                uk_pub = S.public_bytes(_load_key(keyfile))
            else:
                k = self._keygen("uk")
                write_atomic(keyfile, _key_bytes(k))
                uk_pub = S.public_bytes(k)
            held = self._held(self.card())
            c = self._sign_uk_cert(self.op_signer, held, user_id, uk_pub, self._now())
            write_atomic(self._gen_dir() / "uk" / f"{user_id}.json", _json_bytes(c))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return c

    def issue_channel(self, user_id: str, key_pub: bytes, factor: str) -> dict[str, Any]:
        """Certify a channel key (phone, browser) for an existing user. The hub never sees the channel's private key."""
        self._require_op()
        _hex16(user_id, "user_id")
        if factor not in H.FACTORS:
            raise KeysError("malformed", f"factor must be one of {', '.join(H.FACTORS)}")
        if not S.valid_public_key(key_pub):
            raise KeysError("malformed", "key_pub is not a valid P-256 public key")
        self.uk_cert(user_id)  # the user must exist
        try:
            held = self._held(self.card())
            c = self._sign_channel_cert(self.op_signer, held, user_id, key_pub, factor, self._now())
            write_atomic(self._gen_dir() / "ch" / f"{c['o']['channel_id']}.json", _json_bytes(c))
        except StateError as e:
            raise KeysError(e.code, str(e)) from None
        return c


def dumps(o: Any) -> str:
    """The JSON text the CLI prints for public objects."""
    try:
        return json.dumps(o, sort_keys=True, separators=(",", ":"))
    except (TypeError, CanonError) as e:
        raise KeysError("malformed", "unserialisable") from e
