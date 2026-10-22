"""C3b amendment vectors (ticket-format §15): the log-line bytes of hub events and the replay cases of the hub ledger.

``RefLedger`` below is the TEST-SIDE REFERENCE of §15.2/§15.3, built only from the host's existing verifiers
(``wire``, ``snapshot.verify_snapshot``, ``custodial.verify_custodial``, ``identity.hub_actor``). It is not production
code: the C3b implementation makes the model's replay (``model/hubledger.py``) produce the same outcome for every case,
and then this test also runs the cases through the model. Cases marked ``pending`` need the §15.7 ``item_hash`` field
and are strict xfails until C3b lands.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from orch.canon import hashing
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hubclient import HubRefused, wire
from orch.hubclient import custodial as C
from orch.hubclient.snapshot import verify_snapshot
from orch.model import Refusal
from orch.model.identity import hub_actor

from .vec import VECTORS_DIR, pub

X: dict[str, Any] = json.loads((VECTORS_DIR / "c3b_ledger.json").read_text())
WS, HUB = X["workspace_id"], X["hub_id"]
DEV = X["dev_mode"]


class RefLedger:
    """The hub ledger at one position of the workspace log (§15.2): pinned root (from ``hub.joined``), the newest
    pinned card, the revoked op certs, the newest pinned snapshot and the union of every pinned snapshot's revoked
    channels. Every check takes the event's ``at``, never a reader clock."""

    def __init__(self, hub: dict[str, Any]) -> None:
        self.joined, self.mode = hub["joined"], hub["mode"]
        self.book = hub["aliases"]
        self.user_hub = dict(hub.get("user_hub", {}))  # §15.6: the hub each u_ id first appeared under
        self.member_roles = hub.get("member_roles", {})
        self.shared = hub.get("shared", {})
        self.root = unb64u(X["pinned_root"], 65)
        self.card: dict[str, Any] | None = None  # verified card (wire.verify_card result)
        self.revoked: set[str] = set()
        self.snap: dict[str, Any] | None = None
        self.revoked_channels: set[str] = set()
        self.ids: dict[str, bytes] = {}  # event id -> cj of its signed part (one log in these cases)

    def pin(self, obj: dict[str, Any], at: int) -> str:
        if self.joined is None:
            return "not_joined"
        kind = obj["o"]["kind"]
        rev = frozenset(self.revoked)
        if kind == "hub.op_revoked":
            o = wire.verify_op_revoked(obj, self.root)
            if o["cert_id"] in self.revoked:
                return "already_pinned"
            self.revoked.add(o["cert_id"])
            return "ok"
        if kind == "hub.card":
            if self.card is not None and cj(obj) == cj(self.card["signed"]):
                return "already_pinned"
            self.card = wire.verify_card(
                obj, now_ms=at, dev_mode=DEV, pinned_root=self.root, revoked_op_certs=rev, held=self.card
            )
            return "ok"
        if kind == "hub.snapshot":
            if self.card is None:
                return "no_card"
            held = self._held(at)
            o = verify_snapshot(obj, held, workspace_id=WS, dev_mode=DEV, pinned=self.snap, revoked_op_certs=rev)
            if self.snap is not None and o["version"] == self.snap["version"]:
                return "already_pinned"
            self.snap = o
            self.revoked_channels |= set(o["revoked_channels"])
            return "ok"
        return "malformed"

    def _held(self, at: int) -> dict[str, Any]:
        assert self.card is not None
        return wire.verify_card(
            self.card["signed"],
            now_ms=at,
            dev_mode=DEV,
            pinned_root=self.root,
            revoked_op_certs=frozenset(self.revoked),
        )

    def custodial(self, event: dict[str, Any], log: str, at: int) -> tuple[str, str | None]:
        if self.joined is None:
            return "not_joined", None
        signed = cj({k: v for k, v in event.items() if k not in C.NOT_SIGNED} | {"sig": event["sig"]})
        if event["id"] in self.ids:  # §15.4: dedup BEFORE verification (certs may have expired since)
            return ("duplicate" if self.ids[event["id"]] == signed else "replay"), None
        if self.card is None:
            return "no_card", None
        if self.snap is None:
            return "unknown_snapshot", None
        held = self._held(at)
        snap = {**self.snap, "revoked_channels": sorted(self.revoked_channels)}
        own = self.book.get(self.joined, {})
        granted = frozenset(own.values())
        ws = SimpleNamespace(hub={"status": "joined", "hub_id": self.joined, "alias_book": self.book})
        user = event["actor"]["id"][2:]
        who = hub_actor(ws, self.joined, user)
        roles = (
            {} if isinstance(who, Refusal) else {u: self.member_roles.get(who, "viewer") for u in granted if u == user}
        )
        v = C.verify_custodial(
            event, workspace_id=WS, log=log, held=held, snapshot=snap, now_ms=at, dev_mode=DEV, mode=self.mode,
            granted_users=granted, member_roles=roles, revoked_op_certs=frozenset(self.revoked),
        )  # fmt: skip
        assert v["user_id"] == user
        own_alias = user in self.book.get(self.joined, {}).values()
        first = self.user_hub.get(user, self.joined)
        if isinstance(who, Refusal) or (first != self.joined and not own_alias):
            return "foreign_user", None  # §15.6, after the chain: a hub may not mint another hub's user ids
        self.user_hub.setdefault(user, self.joined)
        if self.mode == "selective":  # §15.7, admission only (shares are host state, not log)
            if self.shared.get(event.get("item_hash")) != log:
                return "not_granted", None
        self.ids[event["id"]] = signed
        return "ok", who


def run_case(case: dict[str, Any]) -> list[tuple[str, str | None]]:
    led = RefLedger(case["hub"])
    out: list[tuple[str, str | None]] = []
    for st in case["steps"]:
        try:
            if "pin" in st:
                out.append((led.pin(X["objects"][st["pin"]], st["at_ms"]), None))
            else:
                out.append(led.custodial(X["events"][st["custodial"]], X["ticket"], st["at_ms"]))
        except HubRefused as e:
            out.append((e.code, None))
    return out


CASES = [
    pytest.param(c, id=c["name"], marks=[pytest.mark.xfail(strict=True, reason=c["pending"])] if "pending" in c else [])
    for c in X["replay"]
]


@pytest.mark.parametrize("case", CASES)
def test_replay_case(case: dict[str, Any]) -> None:
    got = run_case(case)
    want = [(st["expect"], st.get("actor")) for st in case["steps"]]
    assert [g[0] for g in got] == [w[0] for w in want]
    for (_, a), (_, w) in zip(got, want, strict=True):
        if w is not None:
            assert a == w


def test_a_later_revocation_does_not_change_an_earlier_outcome() -> None:
    """The point of the ledger: replaying a prefix gives the same outcomes as replaying the whole log."""
    case = next(c for c in X["replay"] if c["name"] == "later_revoked_op_cert_still_replays")
    whole = run_case(case)
    for n in range(1, len(case["steps"]) + 1):
        assert run_case({**case, "steps": case["steps"][:n]}) == whole[:n]


@pytest.mark.parametrize("name", sorted(X["lines"]))
def test_log_line_bytes(name: str) -> None:
    v = X["lines"][name]
    ev = v["event"]
    assert hashing.event_line(ev).hex() == v["line_hex"]
    assert hashing.parse_event_line(bytes.fromhex(v["line_hex"])) == ev
    assert hashing.event_head(ev) == v["head"]
    body = {k: x for k, x in ev.items() if k != "host_sig"}
    signed = hashing.LABELS["sig_host_event"].encode() + cj(
        {"contract": 1, "suite": 2, "workspace_id": WS, "log": v["log"], "event": body}
    )
    assert signed.hex() == v["host_signed_hex"]
    assert S.verify(pub(X["wsk"]), unb64u(ev["host_sig"], 64), signed)


def test_the_custodial_line_is_the_delivered_event_plus_host_fields_only() -> None:
    """§15.1: nothing is stored that the hub did not sign except seq, at, prev, ws_seq, host_sig; the UK signature
    and the channel proof still verify over the stored line."""
    line = X["lines"]["custodial_gate_approved"]["event"]
    e1 = X["events"]["e1"]
    assert set(line) - set(e1) == set(C.HOST_FIELDS)
    assert {k: v for k, v in line.items() if k not in C.HOST_FIELDS} == e1
    held = wire.verify_card(X["objects"]["card_1"], now_ms=1_790_000_001_000, dev_mode=DEV)
    snap = X["objects"]["snap_v3"]["o"]
    delivered = {k: v for k, v in line.items() if k not in C.HOST_FIELDS}
    v = C.verify_custodial(
        delivered, workspace_id=WS, log=X["ticket"], held=held, snapshot=snap, now_ms=1_790_000_001_000, dev_mode=DEV
    )
    assert v["user_id"] == line["actor"]["id"][2:]
    assert "device" not in line["actor"] and line["actor"]["via"] == line["channel"]


def test_the_ledger_line_carries_the_signed_object_verbatim() -> None:
    line = X["lines"]["hub_pinned_snapshot"]["event"]
    assert line["type"] == "hub.pinned" and line["actor"] == {"kind": "host"}
    assert line["object"] == X["objects"]["snap_v3"] and line["hub_id"] == HUB
    assert not {"sig", "auth", "roster_v"} & set(line)


def test_every_event_vector_verifies_its_own_signatures() -> None:
    """The vectors only differ from valid events in what their case names (both signatures are always good)."""
    for name, e in X["events"].items():
        e2 = copy.deepcopy(e)
        body = {k: v for k, v in e2.items() if k not in C.NOT_SIGNED}
        uk = unb64u(e2["uk_cert"]["o"]["uk_pub"], 65)
        assert S.verify(uk, unb64u(e2["sig"], 64), C.custodial_label(e2["type"]) + C._context(WS, X["ticket"], body)), (  # noqa: SLF001
            name
        )
