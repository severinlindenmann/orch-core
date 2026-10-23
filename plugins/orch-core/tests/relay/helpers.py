"""Shared fakes for the R1 session tests: holder store, alarm sink, fake monotonic clock, a WSK key, sessions."""

from __future__ import annotations

import random
from typing import Any

from orch import crypto
from orch.net import http
from orch.relay import retry
from orch.relay.directory import Directory
from orch.relay.session import Session
from orch.wsproto import sign

from . import corpus

WS = corpus.IDS["workspace_a"]
V_ORIGIN = "https://relay.dev.severin.io"  # the vectors' and fixtures' relay origin
WSK_PRIV = corpus.priv("wsk_a.sig")
WSK_PUB = corpus.pub("wsk_a.sig")
OWNER_PK = corpus.pub("person_alice.sig")
CP_SUCCESS = corpus.case("checkpoint_get", "success")["response"]["json"]
GENESIS_PIN, GENESIS_CERT = CP_SUCCESS["genesis"]["pin"], CP_SUCCESS["genesis"]["cert"]
GENESIS = GENESIS_PIN["o"]["genesis"]


class Key:
    def __init__(self, priv=WSK_PRIV):
        self.priv, self.count = priv, 0

    @property
    def public_key(self) -> bytes:
        return crypto.public_bytes(self.priv)

    def sign(self, payload: bytes) -> bytes:
        self.count += 1
        return crypto.sign(self.priv, payload)


class MemLedger:
    """An in-memory :class:`orch.wsproto.state.SignLedger` (the file-backed one is tested in test_state)."""

    def __init__(self):
        self.entries: dict[tuple[str, str], tuple[str, str]] = {}

    def get(self, kind, key):
        return self.entries.get((kind, key))

    def keys(self, kind):
        return sorted(k for (kd, k) in self.entries if kd == kind)

    def record(self, kind, key, digest, sig):
        self.entries[(kind, key)] = (digest, sig)


class Holder:
    """An in-memory :class:`orch.wsproto.state.HolderStore`. ``Holder()`` never held; ``Holder(gen=g)`` was the last
    holder at ``g`` under this instance id. ``saved`` lists the saved gens, ``records`` the saved records."""

    def __init__(self, gen=None, *, last_holder=True, instance_id=None, pending=None, iid="0" * 32):
        from orch.wsproto.state import HolderRecord

        self.iid = iid
        self.rec = (
            None if gen is None and pending is None else HolderRecord(last_holder, gen, instance_id or iid, pending)
        )
        self.saved, self.records = [], []
        self.history = None  # history_problem(): None, or the cause (a linked directory missing holder.json)

    @property
    def gen(self):
        return None if self.rec is None else self.rec.gen

    def instance_id(self):
        return self.iid

    def binding_problem(self):
        return None

    def regenerate(self):
        self.iid = "f" * 32
        return self.iid

    def load(self):
        return self.rec

    def history_problem(self):
        return self.history

    def save(self, rec):
        self.rec = rec
        self.records.append(rec)
        self.saved.append(rec.gen)


class Alarms(list):
    def __call__(self, code: str, detail: str) -> None:
        self.append((code, detail))

    def codes(self):
        return [c for c, _ in self]


class Clock:
    def __init__(self, sim=None):
        self.t, self.sim, self.slept = 5000.0, sim, []

    def mono(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s
        if self.sim is not None:
            self.sim.advance(s)

    def advance(self, s):
        self.t += s
        if self.sim is not None:
            self.sim.advance(s)


OPEN_CONNS: list[http.RelayConn] = []  # closed after each test by tests/relay/conftest.py


def session(
    transport_origin: str,
    link_origin: str | None = None,
    *,
    key: Key | None = None,
    holder: Holder | None = None,
    alarms: Alarms | None = None,
    clock: Clock | None = None,
    ws: str = WS,
    stop=None,
    relay_info=None,
) -> Session:
    s = sign.RelaySigner(key or Key(), sign.LinkContext(workspace_id=ws, origin=link_origin or transport_origin))
    clock = clock or Clock()
    conn = http.RelayConn(transport_origin)
    OPEN_CONNS.append(conn)
    return Session(
        conn,
        s,
        holder=holder if holder is not None else Holder(),
        alarm=alarms if alarms is not None else Alarms(),
        mono=clock.mono,
        sleep=clock.sleep,
        stop=stop,
        relay_info=relay_info,
    )


def directory(sess: Session) -> Directory:
    return Directory(sess, owner_pk_pub=OWNER_PK, wsk_pub=WSK_PUB)


def make_card(origin: str, *, card_seq: int = 1) -> dict[str, Any]:
    """The vector card of workspace A, re-pointed at ``origin`` and signed by its WSK."""
    base = corpus.S2["card"]["card"]
    o = {**base["o"], "relay_url": origin, "card_seq": card_seq}
    link = sign.LinkContext(workspace_id=WS, origin=origin)
    sig = sign.RelaySigner(Key(), link, ledger=MemLedger()).card(o)
    return {"delegation": base["delegation"], "o": o, "sig": sig, "sealed": base["sealed"]}


def retrier(clock: Clock, alarms: Alarms | None = None) -> retry.Retrier:
    return retry.Retrier(sleep=clock.sleep, alarm=alarms if alarms is not None else Alarms(), rng=random.Random(3))
