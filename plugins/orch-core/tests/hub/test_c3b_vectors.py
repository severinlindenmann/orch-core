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
#: what policies.factor_floor returns for a policy without a D66 ``factor`` key (§15.4); the reference has no policy
NO_FLOOR = "password+totp"


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
        self.members: dict[str, str] | None = hub.get("members")  # p_ -> local role (None: not modelled)
        self.decisions: dict[str, tuple[str, bool]] = {}  # decision event id -> (signer key, consumed)
        self.lines = 0  # hub.pinned lines written
        self.info: dict[str, Any] = {}

    def pin(self, obj: dict[str, Any], at: int) -> str:
        if self.joined is None:
            return "not_joined"
        kind = obj["o"]["kind"]
        rev = frozenset(self.revoked)
        if kind == "hub.op_revoked":
            o = wire.verify_op_revoked(obj, self.root)
            if o["cert_id"] in self.revoked:
                return "already_pinned"
            if len(self.revoked) >= 64:
                return "too_many"  # §15.2 caps
            self.revoked.add(o["cert_id"])
            self.lines += 1
            return "ok"
        if kind == "hub.card":
            if self.card is not None and cj(obj) == cj(self.card["signed"]):
                return "already_pinned"
            self.card = wire.verify_card(
                obj, now_ms=at, dev_mode=DEV, pinned_root=self.root, revoked_op_certs=rev, held=self.card
            )
            self.lines += 1
            return "ok"
        if kind == "hub.snapshot":
            if self.card is None:
                return "no_card"
            held = self._held(at)
            o = verify_snapshot(obj, held, workspace_id=WS, dev_mode=DEV, pinned=self.snap, revoked_op_certs=rev)
            if self.snap is not None and o["version"] == self.snap["version"]:
                return "already_pinned"
            if len(cj(obj)) > 64 * 1024:
                return "too_large"
            self.snap = o
            new = set(o["revoked_channels"]) - self.revoked_channels
            self.revoked_channels |= new
            # §15.4 (C3b-2): a newly revoked channel voids its open (not consumed) counting decisions
            keys = {f"hub:{self.joined}/{c}" for c in new}
            self.info["voids"] = sorted(i for i, (k, done) in self.decisions.items() if k in keys and not done)
            for i in self.info["voids"]:
                del self.decisions[i]
            self.lines += 1
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
            granted_users=granted, member_roles=roles, revoked_op_certs=frozenset(self.revoked), min_factor=NO_FLOOR,
        )  # fmt: skip
        assert v["user_id"] == user
        own_alias = user in self.book.get(self.joined, {}).values()
        first = self.user_hub.get(user, self.joined)
        if isinstance(who, Refusal) or (first != self.joined and not own_alias):
            return "foreign_user", None  # §15.6, after the chain: a hub may not mint another hub's user ids
        role = MAPPED[v["floor"]] if self.mode == "full" else self.member_roles.get(who)
        if self.members is not None and who.startswith("p_"):  # §15.4 role_of (C3b-1)
            local = self.members.get(who)
            if local is None:
                return "forbidden_role", None  # removed from the roster: never the hub role
            role = max(local, role, key=RANK.index) if self.mode == "full" else local
        self.info["role"] = role
        self.user_hub.setdefault(user, self.joined)
        if self.mode == "selective":  # §15.7, admission only (shares are host state, not log)
            if self.shared.get(event.get("item_hash")) != log:
                return "not_granted", None
        self.ids[event["id"]] = signed
        if event["type"] in C.DECISION_TYPES:
            self.decisions[event["id"]] = (f"hub:{self.joined}/{event['channel']}", False)
        return "ok", who

    def join(self, aliases: dict[str, str]) -> str:
        """§15.6 (C3b-5): a hub.joined alias may not take an id another hub used, unless both books name one person."""
        for person, user in aliases.items():
            first = self.user_hub.get(user)
            if first is not None and first != self.joined and self.book.get(first, {}).get(person) != user:
                return "foreign_user"
        for person, user in aliases.items():
            self.book.setdefault(self.joined, {})[person] = user
            self.user_hub.setdefault(user, self.joined)
        return "ok"

    def deliver(self, event: dict[str, Any], holds: dict[str, Any], log: str, at: int) -> str:
        """§15.2/§15.5: build the pins the event needs on a scratch copy; commit pins + event together or nothing."""
        tmp = copy.deepcopy(self)
        if tmp.snap is None or event["snapshot_version"] > tmp.snap["version"]:
            r = tmp.pin(holds, at)
            if r not in ("ok", "already_pinned"):
                return "unavailable"
        try:
            r, _ = tmp.custodial(event, log, at)
        except HubRefused as e:
            r = e.code
        if r == "ok":
            self.__dict__.update(tmp.__dict__)
        return r


RANK = ["owner", "maintainer", "member", "viewer"]  # §15.4 total order, highest first
MAPPED = {"admin": "owner", "edit": "member", "read": "viewer"}


def _step(led: RefLedger, st: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    led.info = {}
    try:
        if "pin" in st:
            r = led.pin(X["objects"][st["pin"]], st["at_ms"])
        elif "custodial" in st:
            r, who = led.custodial(X["events"][st["custodial"]], X["ticket"], st["at_ms"])
            led.info["actor"] = who
        elif "deliver" in st:
            for _ in range(st.get("repeat", 1)):
                r = led.deliver(X["events"][st["deliver"]], X["objects"][st["holds"]], X["ticket"], st["at_ms"])
        elif "join" in st:
            r = led.join(st["join"])
        elif "consume" in st:
            led.decisions = {i: (k, True) for i, (k, _) in led.decisions.items()}
            r = "ok"
        else:
            r = "ok"
    except HubRefused as e:
        r = e.code
    led.info["ledger_lines"] = led.lines
    return r, dict(led.info)


def run_case(case: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    led = RefLedger(copy.deepcopy(case["hub"]))
    return [_step(led, st) for st in case["steps"]]


CASES = [
    pytest.param(c, id=c["name"], marks=[pytest.mark.xfail(strict=True, reason=c["pending"])] if "pending" in c else [])
    for c in X["replay"]
]


@pytest.mark.parametrize("case", CASES)
def test_replay_case(case: dict[str, Any]) -> None:
    got = run_case(case)
    assert [g[0] for g in got] == [st["expect"] for st in case["steps"]]
    for (_, info), st in zip(got, case["steps"], strict=True):
        for k in ("actor", "role", "voids", "ledger_lines"):
            if k in st:
                assert info.get(k) == st[k], (st, k, info)


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
        delivered,
        workspace_id=WS,
        log=X["ticket"],
        held=held,
        snapshot=snap,
        now_ms=1_790_000_001_000,
        dev_mode=DEV,
        min_factor=NO_FLOOR,
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


# --- the second runner: the same cases through the MODEL (ticket-format §15.13 task 3) -----------------------------
#
# The model decides the order and the state (``hubledger.apply_pin``, ``authz.authorize_hub``, ``identity``); the
# crypto is injected through the three ``Verifier`` methods. ``HubAdapter`` is the test-side stand-in for the host
# Verifier of task 5: it is built only from the production verifiers the reference above uses. It must agree with the
# reference on every case. Not modelled here: the admission steps (dedup by id, the pins riding on a delivery, the
# ``item_hash`` check of §15.7, task 6) are done by the runner exactly as ``RefLedger`` does them, and the voiding of
# open decisions at a channel-revoking pin is task 4 (``generations``), so ``voids`` is not asserted here.

import datetime  # noqa: E402

from orch.model import authz, hubledger, workspace  # noqa: E402
from orch.model import identity as ident  # noqa: E402
from orch.model.types import Core, Member, WsCore  # noqa: E402


class HubAdapter:
    """The three C3b ``Verifier`` methods over ``orch.hubclient``; ``HubRefused`` becomes the contract code."""

    def verify_custodial_sigs(self, event: Any, *, workspace_id: str, log: str) -> bool:
        try:
            body = {k: v for k, v in event.items() if k not in C.NOT_SIGNED}
            uk = unb64u(event["uk_cert"]["o"]["uk_pub"], 65)
            ch = unb64u(event["channel_cert"]["o"]["key_pub"], 65)
            ok = S.verify(
                uk,
                unb64u(event["sig"], 64),
                C.custodial_label(event["type"]) + C._context(workspace_id, log, body),  # noqa: SLF001
            )
            return ok and S.verify(
                ch,
                unb64u(event["channel_proof"], 64),
                C.SIG_CHANNEL_PROOF + C.event_digest(workspace_id, log, body),
            )
        except (HubRefused, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _held(card: Any, at_ms: int, dev: bool, revoked: frozenset[str]) -> dict[str, Any]:
        return wire.verify_card(
            card, now_ms=at_ms, dev_mode=dev, pinned_root=unb64u(X["pinned_root"], 65), revoked_op_certs=revoked
        )

    def verify_hub_pin(self, obj: Any, *, root: str, card: Any, revoked: frozenset[str], snapshot: Any,
                       workspace_id: str, at_ms: int, dev: bool) -> str | None:  # fmt: skip
        try:
            kind = obj["o"]["kind"]
            if kind == "hub.op_revoked":
                wire.verify_op_revoked(obj, unb64u(root, 65))
            elif kind == "hub.card":
                old = None
                if card is not None:  # the card as it was verified when pinned (no revocation, its own issue time)
                    old = wire.verify_card(
                        card, now_ms=card["op_cert"]["o"]["issued_ms"] + 1, dev_mode=dev, pinned_root=unb64u(root, 65)
                    )
                wire.verify_card(
                    obj, now_ms=at_ms, dev_mode=dev, pinned_root=unb64u(root, 65), revoked_op_certs=revoked, held=old
                )
            elif kind == "hub.snapshot":
                held = self._held(card, at_ms, dev, revoked)
                verify_snapshot(
                    obj, held, workspace_id=workspace_id, dev_mode=dev, pinned=snapshot["o"] if snapshot else None,
                    revoked_op_certs=revoked,
                )  # fmt: skip
            else:
                return "malformed"
        except HubRefused as e:
            return e.code
        return None

    def verify_hub_actor(self, event: Any, *, workspace_id: str, log: str, card: Any, revoked: frozenset[str],
                         snapshot: Any, revoked_channels: frozenset[str], at_ms: int, dev: bool, mode: str,
                         member_role: str | None) -> str | None:  # fmt: skip
        try:
            if card is None:
                return "no_card"
            if snapshot is None:
                return "unknown_snapshot"
            held = self._held(card, at_ms, dev, revoked)
            snap = {**snapshot["o"], "revoked_channels": sorted(revoked_channels)}
            user = event["actor"]["id"][2:]
            delivered = {k: v for k, v in event.items() if k not in C.HOST_FIELDS}  # a log line has the host fields
            C.verify_custodial(
                delivered, workspace_id=workspace_id, log=log, held=held, snapshot=snap, now_ms=at_ms, dev_mode=dev,
                mode=mode, granted_users=frozenset({user}) if member_role is not None else frozenset(),
                member_roles={user: member_role} if member_role is not None else {},
                revoked_op_certs=revoked, min_factor=NO_FLOOR,
            )  # fmt: skip
        except HubRefused as e:
            return e.code
        return None


def _stamp(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms // 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ModelLedger:
    """One case run through the model: ``ws.hub`` as ``hub.joined`` and ``hub.pinned`` build it."""

    def __init__(self, hub: dict[str, Any]) -> None:
        joined, book = hub["joined"], {k: dict(v) for k, v in hub["aliases"].items()}
        self.ws = WsCore(created=True, workspace_id=WS)
        persons = {p for al in book.values() for p in al} | {p[2:] for p in hub.get("members", {})}
        roles = hub.get("members")
        self.ws.members = {
            "p_" + p: Member("p_" + p, p[:4], "owner", "k") for p in persons if roles is None or "p_" + p in roles
        }
        for p, r in {**(roles or {}), **hub.get("member_roles", {})}.items():
            self.ws.members[p] = Member(p, p[2:6], r, "k")
        self.ws.members["p_" + "0" * 32] = Member("p_" + "0" * 32, "own", "owner", "k")
        if joined is not None:
            self.ws.hub = {
                "hub_id": joined, "hk_root_pub": X["pinned_root"], "mode": hub["mode"], "binding_seq": 1,
                "status": "joined", "aliases": dict(book.get(joined, {})), "alias_book": book,
                "user_hub": dict(hub.get("user_hub", {})), "ledger": {joined: hubledger.new_ledger(X["pinned_root"])},
                **({"member": {"kind": "hub", "hub_id": joined}} if hub["mode"] == "full" else {}),
            }  # fmt: skip
        self.v = HubAdapter()
        self.ids: dict[str, bytes] = {}
        self.lines = 0
        self.info: dict[str, Any] = {}

    def pin(self, obj: dict[str, Any], at: int, ws: WsCore | None = None) -> str:
        ws = ws or self.ws
        e = {"hub_id": X["hub_id"], "object": obj, "at": _stamp(at)}
        r = hubledger.apply_pin(ws, e, self.v, dev=DEV)  # type: ignore[arg-type]
        if r is None:
            self.lines += ws is self.ws
            return "ok"
        return r.detail

    def custodial(self, event: dict[str, Any], log: str, at: int, ws: WsCore | None = None) -> str:
        ws = ws or self.ws
        signed = cj({k: v for k, v in event.items() if k not in C.NOT_SIGNED} | {"sig": event["sig"]})
        if ws.hub and event["id"] in self.ids:  # admission step 2: dedup before verification
            return "duplicate" if self.ids[event["id"]] == signed else "replay"
        ws.roster_v = event["roster_v"]  # the event's own view: roster_v is checked by the type rules elsewhere
        line = {**event, "at": _stamp(at)}
        r = authz.authorize_hub(Core(ws=ws), log, line, self.v, dev=DEV)  # type: ignore[arg-type]
        if r is not None:
            return r.detail if r.code.value.startswith("hub.") else r.code.value
        sg = ident.signer(ws, line)
        assert not hasattr(sg, "code")
        self.info = {"actor": sg.person, "role": sg.role}
        self.ids[event["id"]] = signed
        return "ok"

    def join(self, aliases: dict[str, str]) -> str:
        h = self.ws.hub
        self.ws.hub = {**h, "status": "left"}  # hub.left came first: the model's own rules for a (re)join
        e = {
            "type": "hub.joined", "actor": {"kind": "person", "id": "p_" + "0" * 32}, "hub_id": h["hub_id"],
            "hk_root_pub": X["pinned_root"], "mode": h["mode"], "binding_seq": 2,
            "aliases": [{"person_id": p, "user_id": u} for p, u in aliases.items()],
        }  # fmt: skip
        r = workspace.hub_joined(self.ws, e)
        if r is not None:
            self.ws.hub = h
            return r.detail
        return "ok"

    def deliver(self, event: dict[str, Any], holds: dict[str, Any], log: str, at: int) -> str:
        tmp = copy.deepcopy(self.ws)
        snap = hubledger.ledger_of(tmp, X["hub_id"])["snapshot"]  # type: ignore[index]
        if snap is None or event["snapshot_version"] > snap["o"]["version"]:
            if self.pin(holds, at, tmp) not in ("ok", "already_pinned"):
                return "unavailable"
        r = self.custodial(event, log, at, tmp)
        if r == "ok":
            self.ws = tmp
            self.lines += 1 if (snap is None or event["snapshot_version"] > snap["o"]["version"]) else 0
        return r


def run_model_case(case: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    led = ModelLedger(copy.deepcopy(case["hub"]))
    out = []
    for st in case["steps"]:
        led.info = {}
        if "pin" in st:
            r = led.pin(X["objects"][st["pin"]], st["at_ms"])
        elif "custodial" in st:
            r = led.custodial(X["events"][st["custodial"]], X["ticket"], st["at_ms"])
        elif "deliver" in st:
            for _ in range(st.get("repeat", 1)):
                r = led.deliver(X["events"][st["deliver"]], X["objects"][st["holds"]], X["ticket"], st["at_ms"])
        elif "join" in st:
            r = led.join(st["join"])
        else:
            r = "ok"  # consume: task 4
        led.info["ledger_lines"] = led.lines
        out.append((r, dict(led.info)))
    return out


MODEL_CASES = [
    pytest.param(
        c, id=c["name"], marks=[pytest.mark.skip(reason="admission item check: task 6")] if "pending" in c else []
    )
    for c in X["replay"]
]


@pytest.mark.parametrize("case", MODEL_CASES)
def test_model_replay_case_agrees_with_the_reference(case: dict[str, Any]) -> None:
    got = run_model_case(case)
    assert [g[0] for g in got] == [st["expect"] for st in case["steps"]]
    for (_, info), st in zip(got, case["steps"], strict=True):
        for k in ("actor", "role", "ledger_lines"):  # "voids": task 4
            if k in st:
                assert info.get(k) == st[k], (st, k, info)


@pytest.mark.parametrize("case", [c for c in X["replay"] if "pending" not in c], ids=lambda c: c["name"])
def test_model_and_reference_give_the_same_outcomes(case: dict[str, Any]) -> None:
    assert [r for r, _ in run_model_case(case)] == [r for r, _ in run_case(case)]
