"""DEV ONLY, never shipped: `human.*` of host contract v1.4 section 0.8.1 for the disposable dev workspace.

This is a stand-in until the real host (H5a) serves the same ops. It speaks exactly the v1.4 shapes:

* `human.prepare {op, args, signer?, handoff?}` builds the unsigned events from the P1 model, judges them, and returns
  `{request, op, workspace_id, log, events, material, review_digest, fingerprint, origin}`;
* `human.get {request_id, signer?}` returns the request (binding the device when a signer is named);
* `human.pending {code?, request_id?}` lists the person's requests with their state;
* `human.cancel {request_id}`; `human.submit {request_id, events}` verifies the signed events against the prepared
  ones, appends them through the P1 store (which verifies the signatures for real) and keeps a tombstone.

The stub holds no key and signs nothing. Requests live in memory and do not survive a restart of the stub. It serves
a person's own requests only (one peer, one person). `move` is not served (it waits for H6b).
"""

import base64
import copy
import dataclasses
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity import new_ulid
from orch.ops.errors import OrchError
from orch.schema import SECTIONS_BY_TYPE
from orch.store import StoreError
from orch.store.render import section_entry, thaw

from orch_dashboard.errors import HostError, fail

from . import review, settingsops

TTL = 600
KEPT = 24 * 3600  # an ended request and its idem record are kept this long
MAX_PENDING = 20
REQUEST_ID = re.compile(r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
IDEM = re.compile(r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
DEVICE = re.compile(r"^d_[0-9a-f]{8,64}$")
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
QUESTION = re.compile(r"^Q[1-9][0-9]*$")
GATES = ("requirements", "plan", "code")
RESOLUTIONS = ("wont_do", "duplicate", "obsolete", "other")
SETTABLE = {"labels", "links", "priority", "size", "title", "parent"}
MAX_SECTION = 65536
MAX_TEXT = 4096
WORKSPACE_BUILDERS = {
    "settings.set": "settings_set",
    "policy.set": "policy_set",
    "member.role": "member_role",
    "member.remove": "member_remove",
    "device.remove": "device_remove",
    "grant.revoke": "grant_revoke",
}


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def invalid(message: str):
    return fail("invalid.input", message, exit=5)


class _AcceptAll:
    """The verifier of the pre-check only: it judges everything but the signatures, which do not exist yet."""

    def verify_person(self, event: Any, context: Any) -> bool:
        return True

    def verify_host(self, event: Any, **kw: Any) -> bool:
        return True

    def verify_embedded(self, event: Any, **kw: Any) -> bool:
        return True

    def genesis_failure(self, event: Any) -> str | None:
        return None


def orch_error(exc: StoreError) -> OrchError:
    from orch.cli.store_errors import TABLE, to_orch_error

    return to_orch_error(exc, declared=set(TABLE.values()))


def judge(store, event: dict[str, Any], log: str) -> None:
    """Judge `event` with the model as `Store.append` will, without its signature."""
    from orch.model import Refusal, preview

    store.refresh()
    stamp_ = store.peek_stamp(log)  # an unknown log (a new ticket) is stamped seq 1
    state = store.state
    state = dataclasses.replace(state, _ctx=dataclasses.replace(state._ctx, verifier=_AcceptAll(), admit=True))
    probe = copy.deepcopy(event)
    probe.update(sig="x", seq=stamp_["seq"], prev=stamp_["prev"], at=stamp_["at"])
    if "ws_seq" in stamp_:
        probe["ws_seq"] = stamp_["ws_seq"]
    result = preview(state, probe, log=log)
    if isinstance(result, Refusal):
        raise orch_error(StoreError.from_refusal(result))


class Requests:
    """The stub's table of human requests and of submit outcomes (a `Requests` per stub host)."""

    def __init__(self, host, clock=time.time):
        self.host = host
        self.clock = clock
        self.table: dict[str, dict[str, Any]] = {}
        self.idems: dict[str, dict[str, Any]] = {}
        # `human.prepare` honours `opts.idem` (host contract 0.7): idem -> {binding, request id, at}. Kept 24 h after
        # the request ended; a host restart never forgets it (written next to the dev state).
        self.prepares: dict[str, dict[str, Any]] = {}
        self.path = Path(host.state) / f"dev-human-requests-{host.ws}.json"
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.table = {r["id"]: r for r in data.get("requests", [])}
        for r in self.table.values():
            if isinstance(r.get("signed"), str):
                r["signed"] = base64.b64decode(r["signed"])  # the submitted bytes were stored as base64
        self.prepares = data.get("prepares", {})

    def save(self) -> None:
        keep = ("appended",)  # store objects of a submit: not state a restart needs
        rows = [{k: v for k, v in r.items() if k not in keep} for r in self.table.values()]
        for r in rows:
            if isinstance(r.get("signed"), bytes):
                r["signed"] = base64.b64encode(r["signed"]).decode()
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"requests": rows, "prepares": self.prepares}, f)
        os.replace(tmp, self.path)

    # -- state -------------------------------------------------------------------------------------------------------
    def state(self, request: dict[str, Any]) -> str:
        if request["state"] in ("prepared", "bound") and self.clock() >= request["expires"]:
            request["state"] = "expired"
        return request["state"]

    def find(self, request_id: Any) -> dict[str, Any]:
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or request_id not in self.table:
            raise fail("not_found", "no such request", exit=2)
        return self.table[request_id]

    def pending_count(self) -> int:
        return sum(1 for r in self.table.values() if self.state(r) in ("prepared", "bound"))

    def view(self, request: dict[str, Any]) -> dict[str, Any]:
        """`Prepared`, as `human.prepare` and `human.get` return it."""
        return {
            "request": {
                "id": request["id"],
                "expires_at": stamp(request["expires"]),
                "state": self.state(request),
                "submitted_at": request.get("submitted_at"),
            },
            "op": request["op"],
            "workspace_id": request["workspace_id"],
            "log": request["log"],
            "events": copy.deepcopy(request["events"]),
            "material": copy.deepcopy(request["material"]),
            "review_digest": request["review_digest"],
            "fingerprint": request["fingerprint"],
            "origin": copy.deepcopy(request["origin"]),
        }

    # -- ops ---------------------------------------------------------------------------------------------------------
    def prepare(
        self, store, views, args: dict[str, Any], opts: dict[str, Any], files: dict[str, bytes] | None = None
    ) -> dict[str, Any]:
        if set(args) - {"op", "args", "signer", "handoff", "sequence"} or not {"op", "args"} <= set(args):
            raise invalid("human.prepare takes op, args, signer?, handoff? and sequence?")
        op, op_args = args["op"], args["args"]
        if not isinstance(op, str) or not isinstance(op_args, dict):
            raise invalid("op must be text and args an object")
        if op == "move":
            raise fail("op.unsupported", "move is not served yet", exit=1)
        if op not in review.EMITS:
            raise fail("op.not_allowed", f"{op[:40]} is not a person operation of the dev stub", exit=3)
        handoff = args.get("handoff", False)
        if type(handoff) is not bool:
            raise invalid("handoff must be true or false")
        signer = args.get("signer")
        if (signer is None) == (not handoff):
            raise invalid("name a signer, or ask for a hand-off, not both and not neither")
        if signer is not None:
            self.check_signer(store, signer)
        dry = opts.get("dry_run") is True
        idem = None if dry else opts.get("idem")
        binding = None
        if idem is not None:
            if not isinstance(idem, str) or not IDEM.fullmatch(idem):
                raise invalid("idem is a_ and 26 characters")
            binding = review.digest_of(
                {"op": op, "args": op_args, "signer": signer, "handoff": handoff, "person": self.host.person,
                 "sequence": args.get("sequence"),
                 "files": {n: review.digest_of(base64.b64encode(d).decode()) for n, d in sorted((files or {}).items())}}
            )  # fmt: skip
            known = self.prepares.get(idem)
            if known is not None:
                if known["binding"] != binding:
                    raise fail("idem.conflict", "this idem was used for other arguments", exit=5)
                request = self.table.get(known["request"])
                state = self.state(request) if request else None
                ended = state not in ("prepared", "bound")
                if request is None or (ended and self.clock() > known["at"] + TTL + KEPT):
                    raise fail("idem.expired", "this idem is only a tombstone now", exit=2)
                return self.view(request)  # the SAME request, in its current state (prepared, bound or ended)
        if not dry and self.pending_count() >= MAX_PENDING:
            raise fail("quota.host", "too many pending sign requests", exit=9)
        person = self.host.person
        member = store.state.workspace.members.get(person)
        if member is None:
            raise fail("peer.denied", exit=3)
        device = self.any_device(store, person)
        if files and op != "member.add":
            raise fail("blob.invalid", f"{op[:40]} takes no files", exit=5)
        # As the host does: `files.<arg>` names an argument that carries the file's logical name (`-`).
        named = {}
        for arg, data in (files or {}).items():
            if not isinstance(op_args.get(arg), str):
                raise fail("blob.invalid", f"files.{arg[:40]} names no file argument of {op[:40]}", exit=5)
            named[op_args[arg]] = data
        events, log, material = Builder(store, views, person, device).build(op, op_args, named)
        for event in events:
            judge(store, with_probe_device(event, device), log)
        if signer is not None:
            for event in events:
                event["actor"]["device"], event["auth"] = signer["device"], signer["auth"]
        if args.get("sequence") is not None:  # B11: one review, N signatures (core orch.host.sequence)
            from orch.host import sequence

            try:
                meta = sequence.check_meta(
                    args["sequence"], op=op, args=op_args, workspace_id=self.host.ws, person=self.host.person
                )
            except sequence.SequenceInvalid as exc:
                raise invalid(f"sequence: {exc}") from None
            if meta["step"] != 0:
                raise invalid("the dev stub prepares the first step of a sequence only")
            material["sequence"] = {"digest": meta["digest"], "of": meta["of"], "intent": meta["intent"]}
        digest = review.digest_of(material)
        now = self.clock()
        request = {
            "id": "hr_" + new_ulid(),
            "op": op,
            "workspace_id": self.host.ws,
            "log": log,
            "events": events,
            "material": material,
            "review_digest": digest,
            "fingerprint": review.fingerprint(digest, events, op),
            "origin": {"uid": os.getuid(), "pid": os.getpid(), "client": "orch-dashboard", "created_at": stamp(now)},
            "created": now,
            "expires": now + TTL,
            "state": "bound" if signer is not None else "prepared",
            "bound": signer,
            "key": (material.get("ticket") or {}).get("key"),
            "signed": None,
            "result": None,
            "error": None,
        }
        out = self.view(request)
        if dry:
            out["request"] = None
            return out
        self.table[request["id"]] = request
        if idem is not None:
            self.prepares[idem] = {"binding": binding, "request": request["id"], "at": now}
        self.save()
        return out

    def check_signer(self, store, signer: Any) -> None:
        if not isinstance(signer, dict) or set(signer) != {"device", "auth"}:
            raise invalid("signer is {device, auth}")
        if not isinstance(signer["device"], str) or not DEVICE.fullmatch(signer["device"]):
            raise invalid("device must be a device id")
        if signer["auth"] not in ("passphrase", "secure-enclave", "convenience"):
            raise invalid("auth must be passphrase or secure-enclave")
        device = store.state.workspace.devices.get(signer["device"])
        if device is None:
            raise fail("device.unknown", "no such device", exit=5)
        if device.person != self.host.person or device.removed or device.revoked:
            raise fail("device.invalid", "that device is not a valid device of yours", exit=5)

    @staticmethod
    def any_device(store, person: str) -> str:
        for d in store.state.workspace.devices.values():
            if d.person == person and not d.removed and not d.revoked:
                return d.id
        raise fail("peer.denied", exit=3)

    def get(self, store, views, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) - {"request_id", "signer"} or "request_id" not in args:
            raise invalid("human.get takes request_id and signer?")
        request = self.find(args["request_id"])
        signer = args.get("signer")
        state = self.state(request)
        if signer is None:
            return self.view(request)
        if state == "expired":
            raise fail("human.request_expired", "this request has expired", exit=3)
        if state in ("submitted", "cancelled"):
            raise fail("human.request_state", f"this request is {state}", exit=3)
        self.check_signer(store, signer)
        if request["bound"] not in (None, signer):
            raise fail("human.request_state", "this request is bound to another device", exit=3)
        # N3: the tree is checked again at each binding (the stub re-judges the events).
        events = copy.deepcopy(request["events"])
        for event in events:
            event["actor"]["device"], event["auth"] = signer["device"], signer["auth"]
            judge(store, event, request["log"])
        request["events"], request["bound"], request["state"] = events, dict(signer), "bound"
        self.save()
        return self.view(request)

    def pending(self, args: dict[str, Any]) -> dict[str, Any]:
        """This person's requests (one stub person), or the one named by `request_id`."""
        if set(args) - {"request_id"}:
            raise invalid("human.pending takes request_id only")
        rows = list(self.table.values())
        if "request_id" in args:
            rows = [self.find(args["request_id"])]
        return {
            "requests": [
                {
                    "id": r["id"],
                    "op": r["op"],
                    "key": r["key"],
                    "created_at": stamp(r["created"]),
                    "submitted_at": r.get("submitted_at"),
                    "expires_at": stamp(r["expires"]),
                    "state": self.state(r),
                    "event_ids": [e["id"] for e in r["events"]] if r["state"] == "submitted" else [],
                    "seq": r["result"]["seq"] if r["state"] == "submitted" and r["result"] else None,
                    **({"error": r["error"]} if r.get("error") else {}),
                }
                for r in rows
            ]
        }

    def cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"request_id"}:
            raise invalid("human.cancel takes request_id")
        request = self.find(args["request_id"])
        state = self.state(request)
        if state in ("prepared", "bound"):
            request["state"] = "cancelled"
            self.save()
            return {"state": "cancelled"}
        return {"state": state if state in ("submitted", "cancelled") else "cancelled"}

    def submit(self, store, args: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"request_id", "events"}:
            raise invalid("human.submit takes request_id and events")
        events = args["events"]
        if not isinstance(events, list) or not 1 <= len(events) <= 16 or not all(isinstance(e, dict) for e in events):
            raise invalid("events is a list of 1 to 16 signed events")
        idem = opts.get("idem")
        if not isinstance(idem, str) or not IDEM.fullmatch(idem):
            raise fail("idem.required", "a write needs opts.idem", exit=5)
        binding = review.digest_of({"request_id": args["request_id"], "events": events})
        record = self.idems.get(idem)
        if record is not None:
            if record["binding"] != binding:
                raise fail("idem.conflict", "this idem was used for another request", exit=5)
            return self.replay(record["answer"])
        request = self.find(args["request_id"])
        state = self.state(request)
        if state == "submitted" and request["signed"] == canonical(events):
            if request.get("error"):
                raise fail(request["error"]["code"], exit=5)
            answer = request["answer"]
            self.idems[idem] = {"binding": binding, "answer": answer}
            return self.replay(answer)
        if state in ("submitted", "cancelled", "prepared"):
            raise fail("human.request_state", f"this request is {state}", exit=3)
        if state == "expired":
            raise fail("human.request_expired", "this request has expired", exit=3)
        try:
            self.append(store, request, events)
        except (HostError, OrchError) as exc:
            code = exc.error["code"] if isinstance(exc, HostError) else exc.code
            # Validation refusals stay bound. An accepted submit whose append fails is terminal, as in §0.8.1.
            if code == "sig.invalid":
                request.pop("submitted_at", None)  # the store also validates the actual signature
            elif request.get("submitted_at"):
                request.update(state="submitted", signed=canonical(events))
            request["error"] = {"code": code}
            self.save()
            raise
        appended = request["appended"]
        seq = appended[-1].event["seq"]
        answer = {"data": {"op": request["op"]}, "key": request["key"], "seq": seq}
        request.update(
            state="submitted",
            signed=canonical(events),
            result={"seq": seq},
            answer=answer,
            error=None,
        )
        self.save()
        self.idems[idem] = {"binding": binding, "answer": answer}
        return self.replay(answer, duplicate=False)

    def append(self, store, request: dict[str, Any], events: list[dict[str, Any]]) -> None:
        prepared = request["events"]
        if len(events) != len(prepared):
            raise fail("human.request_mismatch", "the events differ from the prepared ones", exit=5)
        for signed, bound in zip(events, prepared, strict=True):
            if review.cj({k: v for k, v in signed.items() if k != "sig"}) != review.cj(bound) or not isinstance(
                signed.get("sig"), str
            ):
                raise fail("human.request_mismatch", "the events differ from the prepared ones", exit=5)
        for event in events:
            judge(store, event, request["log"])
        request["submitted_at"] = stamp(self.clock())
        self.save()  # accepted before the first append, never synthesized at read time
        try:
            texts = (request["material"].get("texts") or {}) if request["op"] == "section.set" else {}
            request["appended"] = [
                store.append(dict(e), log=request["log"], body={sid: texts[sid] for sid in e.get("sections") or {}})
                if texts
                else store.append(dict(e), log=request["log"])
                for e in events
            ]
        except StoreError as exc:
            if exc.code == "sig.invalid":
                raise fail("sig.invalid", "the signature does not verify", exit=5) from None
            raise orch_error(exc) from None

    @staticmethod
    def replay(answer: dict[str, Any], duplicate: bool = True) -> dict[str, Any]:
        return {"data": answer["data"], "key": answer["key"], "seq": answer["seq"], "duplicate": duplicate}


def canonical(events: list[dict[str, Any]]) -> bytes:
    return review.cj(events)


def with_probe_device(event: dict[str, Any], device: str) -> dict[str, Any]:
    probe = copy.deepcopy(event)
    probe["actor"]["device"], probe["auth"] = device, "passphrase"
    return probe


class Builder:
    """Unsigned person events from the P1 model, for each person operation of the stub."""

    def __init__(self, store, views, person: str, device: str):
        self.store, self.views, self.person, self.device = store, views, person, device

    # -- shared ------------------------------------------------------------------------------------------------------
    def envelope(self, event: dict[str, Any], log: str, *, based_on: Any = ...) -> dict[str, Any]:
        self.store.refresh()
        return {
            "v": 2,
            "id": new_ulid(),
            "actor": {"kind": "person", "id": self.person, "device": None},
            "auth": None,
            "hash_v": 1,
            "roster_v": self.store.state.workspace.roster_v,
            "based_on": self.store.log_head(log) if based_on is ... else based_on,
            **event,
        }

    def ticket(self, ref: Any):
        if not isinstance(ref, str) or not KEY.fullmatch(ref):
            raise invalid("ref is a ticket key such as DEMO-0043")
        view = self.store.ticket(ref)
        if view is None:
            raise fail("not_found", f"no ticket {ref}", exit=2)
        if view.visibility != "workspace" and self.person not in view.visibility["restricted"]:
            raise fail("not_found", f"no ticket {ref}", exit=2)
        return view

    @staticmethod
    def exact(args: dict[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
        if not required <= set(args) or set(args) - required - optional:
            raise invalid(
                "arguments: "
                + ", ".join(sorted(required))
                + (" (+ " + ", ".join(sorted(optional)) + ")" if optional else "")
            )

    @staticmethod
    def text(value: Any, what: str, *, required: bool = False) -> str | None:
        if value is None:
            if required:
                raise invalid(f"{what} is required")
            return None
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > MAX_TEXT:
            raise invalid(f"{what} is 1 to {MAX_TEXT} bytes of text")
        if not canon.is_clean_text(value):
            raise fail("parse.text", f"{what} has characters orch refuses", exit=6)
        return value

    def basis(self, view, gate: str) -> dict[str, Any]:
        g = view.gates[gate]
        if g.hash is None:
            raise invalid(f"the {gate} gate cannot be hashed right now")
        return {"gate_gen": g.gen, "hash": g.hash, "policy_hash": g.policy_hash}

    def refuse_repeat(self, view, gate: str) -> None:
        g = view.gates[gate]
        if any(
            d.person == self.person and d.gen == g.gen and d.kind in ("approve", "pass") and not d.voided
            for d in g.decisions
        ):
            raise fail("gate.already_approved", f"you already approved {gate} of {view.key}", exit=3)

    @staticmethod
    def sources(view) -> list[dict[str, str]]:
        return [{"repo": e["repo"], "ref": e["ref"], "sha": e["sha"]} for e in view.source_list]

    def gate_material(self, view, gate: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        material = {
            "kind": "gate",
            "gate": gate,
            "input": thaw(view.gates[gate].input or {}),
            "policy": thaw(view.gates[gate].policy),
            "texts": {  # every section the gate binds, an empty one as "" (the real host's material, P1)
                sid: self.store.body_sections(view.uid).get(sid, "")
                for sid in sorted((view.gates[gate].input or {}).get("sections") or {})
            },
            "ticket": {"key": view.key, "uid": view.uid},
        }
        material["review"] = review.reviews_of(events, material)
        return material

    def payload_material(self, view, events, *, texts=None, before=None, ticket=None) -> dict[str, Any]:
        material: dict[str, Any] = {
            "kind": "payload",
            "ticket": ticket if ticket is not None else ({"key": view.key, "uid": view.uid} if view else None),
        }
        if texts:
            material["texts"] = texts
        if before:
            material["before"] = before
        material["review"] = review.reviews_of(events, material)
        return material

    # -- operations --------------------------------------------------------------------------------------------------
    def build(self, op: str, args: dict[str, Any], files: dict[str, bytes] | None = None):
        if op == "member.add":
            return settingsops.member_add(self, args, files or {})
        if op in WORKSPACE_BUILDERS:
            return getattr(settingsops, WORKSPACE_BUILDERS[op])(self, args)
        return getattr(self, "op_" + op.replace(".", "_"))(args)

    def op_approve(self, args):
        self.exact(args, {"ref", "gate"})
        gate = args["gate"]
        if gate not in GATES:
            raise invalid("gate is requirements, plan or code")
        view = self.ticket(args["ref"])
        self.refuse_repeat(view, gate)
        event: dict[str, Any] = {"type": "gate.approved", "gate": gate, **self.basis(view, gate)}
        if gate == "code":
            event["source_sha"] = self.sources(view)
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.gate_material(view, gate, events)

    def op_request_changes(self, args):
        self.exact(args, {"ref", "gate", "message"})
        gate = args["gate"]
        if gate not in (*GATES, "verify"):
            raise invalid("gate is requirements, plan, verify or code")
        view = self.ticket(args["ref"])
        text = self.text(args["message"], "the reason", required=True)
        event = {"type": "gate.changes_requested", "gate": gate, **self.basis(view, gate), "text": text}
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.gate_material(view, gate, events)

    def op_verdict(self, args):
        self.exact(args, {"ref", "outcome"}, {"message"})
        outcome = args["outcome"]
        if outcome not in ("pass", "fail"):
            raise invalid("outcome is pass or fail")
        view = self.ticket(args["ref"])
        text = self.text(args.get("message"), "the reason", required=outcome == "fail")
        if outcome == "pass":
            self.refuse_repeat(view, "verify")
        event: dict[str, Any] = {
            "type": "verdict.given",
            "outcome": outcome,
            **self.basis(view, "verify"),
            "source_sha": self.sources(view),
        }
        if text is not None:
            event["text"] = text
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.gate_material(view, "verify", events)

    def op_answer(self, args):
        self.exact(args, {"ref", "question"}, {"option", "message"})
        qid = args["question"]
        if not isinstance(qid, str) or not QUESTION.fullmatch(qid):
            raise invalid("question is Q1, Q2 ...")
        view = self.ticket(args["ref"])
        q = next((x for x in view.questions if x.id == qid), None)
        if q is None:
            raise fail("not_found", f"{view.key} has no question {qid}", exit=2)
        text = self.text(args.get("message"), "the answer")
        option = args.get("option")
        if option is not None and not isinstance(option, str):
            raise invalid("option is text")
        if option is None and text is None:
            raise invalid("answer with an option, a message, or both")
        event: dict[str, Any] = {"type": "question.answered", "question": qid, "hash": q.hash}
        if option is not None:
            event["option"] = option
        if text is not None:
            event["text"] = text
        events = [self.envelope(event, view.uid)]
        doc = next((x for x in view.fields["questions"] if x["id"] == qid), {})
        material = {"kind": "question", "ticket": {"key": view.key, "uid": view.uid}, "question": thaw(doc)}
        material["review"] = review.reviews_of(events, material)
        return events, view.uid, material

    def op_close(self, args):
        self.exact(args, {"ref"}, {"resolution", "duplicate_of", "message"})
        view = self.ticket(args["ref"])
        resolution = args.get("resolution", "other")
        if resolution not in RESOLUTIONS:
            raise invalid("resolution is wont_do, duplicate, obsolete or other")
        event: dict[str, Any] = {"type": "ticket.closed", "resolution": resolution}
        if args.get("duplicate_of") is not None:
            if resolution != "duplicate":
                raise invalid("duplicate_of goes with the resolution duplicate")
            event["duplicate_of"] = self.ticket(args["duplicate_of"]).key
        text = self.text(args.get("message"), "the reason")
        if text is not None:
            event["text"] = text
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.payload_material(view, events)

    def op_reopen(self, args):
        self.exact(args, {"ref"}, {"message"})
        view = self.ticket(args["ref"])
        event: dict[str, Any] = {"type": "ticket.reopened"}
        text = self.text(args.get("message"), "the reason")
        if text is not None:
            event["text"] = text
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.payload_material(view, events)

    def op_log(self, args):
        self.exact(args, {"ref", "message"})
        view = self.ticket(args["ref"])
        events = [
            self.envelope(
                {"type": "log.added", "text": self.text(args["message"], "the note", required=True)}, view.uid
            )
        ]
        return events, view.uid, self.payload_material(view, events)

    def links_value(self, view, raw):
        """``links={...}`` as the core's ``set`` makes it: the object's keys replace the ticket's, and every repository
        must be in ``settings.repos`` (the model refuses an unknown one at the append)."""
        try:
            doc = canon.loads_strict(raw.encode())
        except (canon.JcsError, ValueError):
            raise invalid("links is a JSON object") from None
        if not isinstance(doc, dict) or set(doc) - {"repos", "branches", "prs", "external"}:
            raise invalid("links is an object with repos, branches, prs and external")
        value = {**thaw(view.fields["links"]), **doc}
        known = self.store.state.workspace.repos
        unknown = [r for r in value["repos"] if r not in known]
        if unknown:
            raise invalid(f"repos not in settings.repos: {unknown}")
        return value

    def op_set(self, args):
        self.exact(args, {"ref", "pairs"})
        pairs = args["pairs"]
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= 4 or not all(isinstance(p, str) for p in pairs):
            raise invalid("pairs is 1 to 4 key=value texts")
        view = self.ticket(args["ref"])
        sets: dict[str, Any] = {}
        before: dict[str, Any] = {}
        for pair in pairs:
            name, sep, value = pair.partition("=")
            if not sep or name not in SETTABLE or f"ticket.{name}" in sets:
                raise invalid(f"key=value with a key of {', '.join(sorted(SETTABLE))}")
            path = f"ticket.{name}"
            if name == "labels":
                labels = sorted({x for x in value.split(",") if x})
                if not labels or not all(LABEL.fullmatch(x) for x in labels):
                    raise invalid("labels are lower-case words, comma separated")
                sets[path] = labels
            elif name == "title":
                title = self.text(value, "the title", required=True)
                if "\n" in title or len(title) > 200:
                    raise invalid("the title is one line, at most 200 characters")
                sets[path] = title
            elif name == "parent":
                if value in ("none", "null"):
                    sets[path] = None
                else:
                    parent = self.ticket(value)  # exists and is visible to the person
                    if parent.key == view.key:
                        raise invalid("a ticket is not its own parent")
                    sets[path] = parent.key
            elif name == "links":
                sets[path] = self.links_value(view, value)
            elif name == "priority":
                if value not in ("low", "medium", "high", "urgent"):
                    raise invalid("priority is low, medium, high or urgent")
                sets[path] = value
            else:
                if value not in ("xs", "s", "m", "l", "xl"):
                    raise invalid("size is xs, s, m, l or xl")
                sets[path] = value
            before[path] = thaw(view.fields[name])
        event = {
            "type": "ticket.updated",
            "set": sets,
            "base_rev": {path: canon.value_hash(value) for path, value in before.items()},
        }
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.payload_material(view, events, before=before)

    def op_section_set(self, args):
        self.exact(args, {"ref", "section", "message"})
        view = self.ticket(args["ref"])
        sid = args["section"]
        if not isinstance(sid, str) or sid not in SECTIONS_BY_TYPE[view.type]:
            raise invalid(f"a {view.type} ticket has no section {str(sid)[:40]}")
        text = args["message"]
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_SECTION:
            raise invalid(f"the section is 1 to {MAX_SECTION} bytes of text")
        if not canon.is_clean_text(text):
            raise fail("parse.text", "the section has characters orch refuses", exit=6)
        text = text.strip("\n")
        before = self.store.body_sections(view.uid).get(sid, "")
        event = {
            "type": "ticket.updated",
            "base_rev": {f"body.{sid}": canon.section_hash(before)},
            "sections": {sid: section_entry(text)},
        }
        events = [self.envelope(event, view.uid)]
        return events, view.uid, self.payload_material(view, events, texts={sid: text}, before={f"body.{sid}": before})

    def op_new(self, args):
        self.exact(args, {"title"}, {"type", "priority", "size", "label", "parent", "message"})
        extras = sorted(set(args) - {"title", "type"})
        if extras:
            # P1's ticket.created holds key, type, title and owner only, and EMITS_V1["new"] is ticket.created alone.
            raise invalid("the dev stub signs the title and type only (contract question: the other fields)")
        title = self.text(args["title"], "the title", required=True)
        if "\n" in title or len(title) > 200:
            raise invalid("the title is one line, at most 200 characters")
        kind = args.get("type", "feature")
        if kind not in ("feature", "bug", "chore", "spike", "epic"):
            raise invalid("type is feature, bug, chore, spike or epic")
        uid, key = new_ulid(), self.store.peek_key()
        event = {"type": "ticket.created", "key": key, "ticket_type": kind, "title": title, "owner": self.person}
        events = [self.envelope(event, uid, based_on=None)]
        ticket = {"key": key, "uid": uid}
        return events, uid, self.payload_material(None, events, ticket=ticket)
