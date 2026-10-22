"""A whole custodial engine on real parts: a real ``HubKeys`` store, ``Access`` + snapshots, a real ``WkSeals`` holding a
wrap built exactly as orch-core builds it, a real audit chain; the workspace view and the channel registry are stubs."""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from orch_hub.access import Access, AccessSnapshots
from orch_hub.audit import AuditLog, AuditStore
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u
from orch_hub.custodial import BuiltEvent, CustodialEngine
from orch_hub.keys import HubKeys
from orch_hub.link.wkseal import WkSeals

WS = "ab" * 16
LOG = "01J9ZP0000000000000000TKT1"
WK = bytes(range(32))
NEW_LOG = "01J9ZP0000000000000000NEW1"
ACTION = {"type": "log.added", "log": LOG, "text": "hello"}
BASE = "sha256:" + "6b" * 32


def core_seal(wk: bytes, hub_id: str, ws: str, epoch: int, to: bytes) -> bytes:
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), to))
    k = HKDF(hashes.SHA256(), 32, eph_pub + to, f"orch/v2/seal|wk|{hub_id}".encode()).derive(ss)
    aad = b"orch/v2/seal-aad|" + bytes([S.SUITE_ID, 2]) + b"wk" + bytes.fromhex(ws) + epoch.to_bytes(4, "big")
    return eph_pub + AESGCM(k).encrypt(bytes(12), wk, aad)


class StubView:
    """Deterministic: the event depends on ``self.text`` and ``self.etype`` only (the stand-in for 'the workspace')."""

    def __init__(self) -> None:
        self.text = "hello"
        self.etype: str | None = None  # None: the action's type
        self.extra: dict[str, Any] = {}
        self.log: str | None = None
        self.calls = 0
        self.seen_wk: bytearray | None = None
        self.seen_wk_copy = b""

    def build(self, wk_e, epoch, workspace_id, user_id, event_id, action) -> BuiltEvent:
        self.calls += 1
        self.seen_wk, self.seen_wk_copy = wk_e, bytes(wk_e)
        core = {
            "type": self.etype or action["type"],
            "text": self.text,
            "based_on": BASE,
            "roster_v": 1,
            "hash_v": 1,
            **self.extra,
        }
        return BuiltEvent(self.log or action.get("log", NEW_LOG), core)


class Channels:
    def __init__(self) -> None:
        self.certs: dict[tuple[str, str], dict[str, Any]] = {}

    def active_channel(self, user_id: str, channel_id: str):
        return self.certs.get((user_id, channel_id))


class Sessions:
    def __init__(self) -> None:
        self.ok = True

    def valid(self, session, user_id, channel_id):
        return self.ok and session == "sess"


class Env:
    def __init__(self, tmp_path, *, role: str | None = "edit", mode: str = "full", ws: str = WS, shares=None) -> None:
        self.ws = ws
        self.keys = HubKeys.init(tmp_path / "hub")
        self.access = Access()
        self.snaps = AccessSnapshots(self.access, self.keys)
        self.user = self.keys.issue_uk()["o"]["user_id"]
        self.access.add_user(self.user, "ann")
        self.access.add_group("team")
        self.access.add_member("team", self.user)
        if role:
            self.access.grant(ws, "team", role)
        d = tmp_path / "seal"
        d.mkdir(mode=0o700)
        self.seals = WkSeals(
            str(d / "wk-seal.db"), hub_id=self.keys.hub_id, hxk_pub=self.keys.hxk_pub, exchange=self.keys.hxk_exchange
        )
        self.seals.accept(ws, 1, core_seal(WK, self.keys.hub_id, ws, 1, self.keys.hxk_pub))
        self.audit = AuditLog(
            AuditStore(),
            self.keys.op_signer,
            self.keys.hub_id,
            lambda: self.keys.op_cert()["o"]["cert_id"],
            op_cert_by_id=self.keys.op_cert_by_id,
            dev_mode=True,
        )
        self.view, self.channels, self.delivered = StubView(), Channels(), []
        self.sessions = Sessions()
        self.mode = mode
        self.clock = [self.keys._now()]
        self.ch_priv = ec.generate_private_key(ec.SECP256R1())
        self.channel = self.new_channel(self.ch_priv)
        self.engine = CustodialEngine(
            keys=self.keys,
            snapshots=self.snaps,
            seals=self.seals,
            view=self.view,
            channels=self.channels,
            sessions=self.sessions,
            audit=self.audit,
            deliver=lambda ws, log, ev: self.delivered.append((ws, log, ev)),
            mode_of=lambda ws: self.mode,
            now_ms=lambda: self.clock[0],
            dev_mode=True,
            shares=shares,
        )

    def new_channel(self, priv, factor: str = "se-unlocked", user: str | None = None) -> str:
        user = user or self.user
        cert = self.keys.issue_channel(user, S.public_bytes(priv), factor)
        cid = cert["o"]["channel_id"]
        self.channels.certs[(user, cid)] = cert
        return cid

    def proof(self, digest: bytes, priv=None) -> str:
        class _S(KS.ChannelSigner):
            pass

        return b64u(_S(priv or self.ch_priv).proof(digest))

    def prepare(self, action=None):
        return self.engine.prepare("sess", self.ws, self.user, self.channel, action or dict(ACTION))

    def close(self) -> None:
        self.seals.close()
