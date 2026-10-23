"""The R3 test kit: a real linked :class:`orch.store.Store` (``tests.store.helpers.Env``, a WSK whose private key the
test holds), a :class:`~tests.relay.pubsim.CheckpointSim` behind the loopback :class:`~tests.relay.mock.MockRelay`, a
real R1 :class:`Session` holding the workspace, the owner-signed genesis pin on the relay, and the
:class:`~orch.relay.publish.Publisher` over a fresh relay state directory, with ``job(fn) = fn()``."""

from __future__ import annotations

import contextlib
import random
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.crypto.labels import L
from orch.net import http as rhttp
from orch.relay.publish import Publisher
from orch.relay.session import Session
from orch.wsproto.state import StateDir
from tests.store.helpers import T0, WS, Env

from .helpers import OPEN_CONNS, Alarms, Clock, Key, session
from .mock import MockRelay
from .pubsim import CheckpointSim


class Kit:
    def __init__(
        self, tmp: Path, *, max_pub: int = 16 << 20, link_cap: int | None = None, info: Any = True,
        tickets: int = 0, events: int = 0,
    ) -> None:  # fmt: skip
        self.tmp = tmp
        self.priv = crypto.generate_private_key()
        self.env = env = Env(tmp)
        env.signer = Key(self.priv)  # type: ignore[assignment]  # the store's host signer; the session signs with it too
        env.wsk_pub = crypto.public_bytes(self.priv)
        env.bootstrap()
        self.store = env.store
        self.store.relay_link({"v": 1, "workspace_id": WS}, guard=contextlib.nullcontext)
        self.uids: list[str] = []
        for t in range(tickets):
            uid = env.new_ticket(title=f"t{t}")
            self.uids.append(uid)
            for i in range(events):
                env.log(uid, f"{t}-{i}")
        self.mock = MockRelay()
        self.origin = self.mock.origin()
        self.sim = CheckpointSim(self.origin, owner_pk=env.owner.pk_pub, wsk_pub=env.wsk_pub, ws=WS, max_pub=max_pub,
                                 info=info, now_ms=(T0 + 3600) * 1000)  # fmt: skip
        self.mock.fallback = self.sim
        self.clock = Clock()
        self.alarms = Alarms()
        self.session: Session = session(self.origin, key=Key(self.priv), ws=WS, clock=self.clock)
        self.session.hold()
        self.put_genesis()
        self.state = StateDir(tmp / "relay-state")
        self.link_cap = link_cap if link_cap is not None else max_pub
        self.diverged: list[Any] = []
        self.publisher = self.make_publisher()

    def put_genesis(self) -> None:
        o = {"v": 2, "suite": 2, "kind": "genesis_pin", "workspace_id": WS,
             "wsk_pub": crypto.b64u(self.env.wsk_pub), "genesis": self.store.genesis}  # fmt: skip
        owner = self.env.owner
        pin = {"o": o, "sig": crypto.b64u(owner.dk_sign(L["sig_genesis_pin"] + canon.dumps(o)))}
        self.sim.genesis[WS] = canon.dumps({"pin": pin, "cert": owner.cert(created=T0 * 1000)})

    def conn(self) -> rhttp.RelayConn:
        c = rhttp.RelayConn(self.origin)
        OPEN_CONNS.append(c)
        return c

    def make_publisher(self, **kw: Any) -> Publisher:
        kw.setdefault("max_publication_bytes", self.link_cap)
        kw.setdefault("conn", self.conn)
        kw.setdefault("mono", self.clock.mono)
        return Publisher(
            self.store, job=lambda fn: fn(), session=self.session, owner_pk_pub=self.env.owner.pk_pub,
            wsk_pub=self.env.wsk_pub, state=self.state, alarm=self.alarms,
            on_diverged=self.diverged.append, now_ms=lambda: self.sim.now,
            rng=random.Random(7), **kw,
        )  # fmt: skip

    def drain(self, limit: int = 60, *, publisher: Publisher | None = None) -> list[tuple[str, float]]:
        """Step until idle (or ``limit`` steps); returns every step's result."""
        p = publisher or self.publisher
        out = []
        for _ in range(limit):
            r = p.step()
            out.append(r)
            if r[0] == "idle":
                return out
        return out

    def held_n(self) -> int | None:
        return None if self.sim.held is None else self.sim.held["o"]["n"]

    def current(self) -> dict[str, Any]:
        """The newest local workspace checkpoint."""
        pos = self.store.relay_position(None)
        assert pos.current is not None
        return pos.current

    def close(self) -> None:
        self.publisher.stop()
        self.state.close()
        self.mock.close()
        if self.store is not None:
            self.store.close()
