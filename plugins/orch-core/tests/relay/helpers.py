"""Shared fakes for the R1 session tests: holder store, alarm sink, fake monotonic clock, a WSK key, sessions."""

from __future__ import annotations

import random
from typing import Any

from orch import crypto
from orch.relay import http, retry, sign
from orch.relay.directory import Directory
from orch.relay.session import Session

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


class Holder:
    def __init__(self, gen: int | None = None):
        self.gen, self.saved = gen, []

    def load(self):
        return self.gen

    def save(self, gen):
        self.gen = gen
        self.saved.append(gen)


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
) -> Session:
    s = sign.RelaySigner(key or Key(), sign.LinkContext(workspace_id=ws, origin=link_origin or transport_origin))
    clock = clock or Clock()
    return Session(
        http.RelayConn(transport_origin),
        s,
        holder=holder if holder is not None else Holder(),
        alarm=alarms if alarms is not None else Alarms(),
        mono=clock.mono,
        sleep=clock.sleep,
        stop=stop,
    )


def directory(sess: Session) -> Directory:
    return Directory(sess, owner_pk_pub=OWNER_PK, wsk_pub=WSK_PUB)


def make_card(origin: str, *, card_seq: int = 1) -> dict[str, Any]:
    """The vector card of workspace A, re-pointed at ``origin`` and signed by its WSK."""
    base = corpus.S2["card"]["card"]
    o = {**base["o"], "relay_url": origin, "card_seq": card_seq}
    sig = sign.RelaySigner(Key(), sign.LinkContext(workspace_id=WS, origin=origin)).card(o)
    return {"delegation": base["delegation"], "o": o, "sig": sig, "sealed": base["sealed"]}


def retrier(clock: Clock, alarms: Alarms | None = None) -> retry.Retrier:
    return retry.Retrier(sleep=clock.sleep, alarm=alarms if alarms is not None else Alarms(), rng=random.Random(3))
