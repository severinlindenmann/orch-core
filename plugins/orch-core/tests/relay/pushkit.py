"""The H13a test kit: a real :class:`orch.store.Store` (``tests.store.helpers.Env``), a fake epoch-key source and
member view, a real :class:`~orch.relay.keys.BridgeKeys` and :class:`~orch.relay.sign.RelaySigner`, an
:class:`~orch.relay.outbound.OutboundStore` over a fresh state directory, and a scripted fake relay behind a fake
session (``call`` records every request and answers from a script)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.crypto import push as cpush
from orch.relay import http as rhttp
from orch.relay.keys import BridgeKeys, Member, MemberList, SealState
from orch.relay.outbound import OutboundStore
from orch.relay.push import PushThread
from orch.relay.retry import RelayRefused
from orch.relay.sign import LinkContext, RelaySigner
from orch.relay.state import StateDir
from orch.store.store import device_standing_ok
from tests.store.helpers import T0, WS, Env

from .helpers import Alarms

WK = {1: b"\x01" * 32, 2: b"\x02" * 32, 3: b"\x03" * 32}
Q_OPTIONS = [{"key": "yes", "label": "yes"}, {"key": "no", "label": "no"}]


class Epochs:
    """A fake :class:`orch.relay.keys.EpochKeySource`: ``current`` is settable, ``gated`` raises the rotation gate."""

    def __init__(self) -> None:
        self.current, self.gated = 1, False
        self.lock = threading.RLock()

    def current_epoch(self) -> int:
        return self.current

    def wk(self, epoch: int) -> bytes | None:
        return WK.get(epoch) if 1 <= epoch <= self.current else None

    def seal_state(self) -> SealState:
        return SealState(self.current, self.gated, self.current + 1)

    def refusal_key(self, epoch: int) -> bytes | None:
        return None

    def is_burned(self, epoch: int) -> bool:
        return False

    @contextlib.contextmanager
    def seal_snapshot(self) -> Iterator[SealState]:
        with self.lock:
            yield self.seal_state()

    @contextlib.contextmanager
    def serialised(self) -> Iterator[None]:
        with self.lock:
            yield


class View:
    """A fake :class:`orch.relay.keys.R6View`: the accepted list is whatever ``members`` says."""

    def __init__(self) -> None:
        self.members: dict[str, Member] = {}
        self.epoch = 1

    def add(self, person_ref: str, device_ref: str, scopes: tuple[str, ...] = ("look", "decide"),
            cert_scopes: tuple[str, ...] = ("look", "decide", "operate", "type")) -> None:  # fmt: skip
        d = device_ref[2:]
        self.members[d] = Member(d, person_ref[2:], scopes, {"o": {"scopes_max": list(cert_scopes)}}, b"\x00" * 32)

    def member_list(self) -> MemberList:
        return MemberList(self.epoch, "sha256:" + "0" * 64, dict(self.members))


class FakeSession:
    """Stands in for :class:`orch.relay.session.Session` towards the push route: ``script`` is a list of replies (a
    dict answers 200, an exception is raised), consumed in order; the last one repeats. ``calls`` has every request."""

    def __init__(self, ws: str = WS) -> None:
        self.workspace_id, self.gen = ws, 1
        self.calls: list[dict[str, Any]] = []
        self.script: list[Any] = []

    def call(self, method: str, path: str, route: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "route": route, **kw})
        reply = self.script.pop(0) if len(self.script) > 1 else (self.script[0] if self.script else None)
        if reply is None:
            targets = kw["body"]["targets"]
            return {"accepted": len(targets)}
        if isinstance(reply, BaseException):
            raise reply
        return reply


def refused(status: int, code: str | None = None, retry_after: int | None = None) -> RelayRefused:
    return RelayRefused(status, code, retry_after=retry_after, route="POST /v2/workspaces/{ws}/push")


class Kit:
    """One workspace with a ticket, an owner device in the member list, and the push machinery over it."""

    def __init__(self, tmp: Path, *, fence_after: int = 0) -> None:
        self.env = env = Env(tmp)
        env.bootstrap()
        self.store = env.store
        self.uid = env.new_ticket(title="Ship it")
        self.epochs = Epochs()
        self.view = View()
        self.view.add(env.owner.ref, env.owner.device)
        self.alarms = Alarms()
        self.now = (T0 + fence_after) * 1000  # the planner's clock (ms); the store's is env.clock
        self.keys = BridgeKeys(self.epochs, bytes.fromhex(WS))
        self.signer = RelaySigner(env.signer, LinkContext(WS, "https://relay.example"), epochs=self.keys)
        self.state_dir = StateDir(tmp / "relay-state")
        self.ob = OutboundStore(self.state_dir, instance_id="i" * 32, now_ms=self.now)
        self.session = FakeSession()
        self.thread = PushThread(
            self.store, ob=self.ob, job=lambda fn: fn(), session=self.session, keys=self.keys, signer=self.signer,
            view=self.view, standing=device_standing_ok, alarm=self.alarms, now_ms=lambda: self.now,
        )  # fmt: skip

    def close(self) -> None:
        self.ob.close()
        self.state_dir.close()

    # -- the clock: the store stamps events with env.clock (seconds), the push side runs on self.now (ms) -----------
    def advance(self, seconds: float) -> None:
        self.env.clock[0] += int(seconds)
        self.now += int(seconds * 1000)

    # -- events --------------------------------------------------------------------------------------------------
    def ask(self, local: str = "Q1", text: str = "deploy now?", to: str = "ticket_owner") -> str:
        """An agent-asked question on the ticket; returns the question's qid."""
        q = {"id": local, "to": to, "text": text, "blocking": True, "kind": "choice", "options": Q_OPTIONS}
        qid = canon.question_id(WS, self.uid, local)
        h = canon.question_hash(qid, self.uid, q)
        self.store.append(
            {"type": "question.asked", "actor": self.env.agent, "question": q, "qid": qid, "hash": h}, log=self.uid
        )
        return qid

    def answer(self, local: str = "Q1", option: str = "yes") -> None:
        cur = next(q for q in self.store.state.tickets[self.uid].questions if q.id == local)
        self.store.append(
            self.env.person_event(self.env.owner, self.uid, "question.answered", question=local, hash=cur.hash,
                                  option=option),
            log=self.uid,
        )  # fmt: skip

    def rollback(self, to_seq: int) -> None:
        """An owner repair: cut the ticket log back to ``to_seq`` and append the signed restore (11A.16.4: a rewritten
        log)."""
        self.store.checkpoint()
        self.store.abandon_tail(self.uid, to_seq)
        self.store.append(
            self.env.person_event(self.env.owner, self.uid, "restore", **self.store.restore_facts(self.uid),
                                  reason="rollback"),
            log=self.uid,
        )  # fmt: skip

    def plan(self) -> int:
        return self.thread.planner.run(self.now)

    def open(self, outer: bytes, epoch: int = 1) -> dict[str, Any]:
        """The service worker's view of a sent outer (protocol §9 steps 1 to 6)."""
        keys = {WS: cpush.PushKeys(self.env.wsk_pub, {epoch: WK[epoch]})}
        return cpush.open_push(outer, keys, self.now)


def is_transport_error() -> rhttp.TransportError:
    return rhttp.TransportError("connect", "POST /v2/workspaces/{ws}/push")


__all__ = ["WK", "FakeSession", "Kit", "is_transport_error", "refused", "crypto"]
