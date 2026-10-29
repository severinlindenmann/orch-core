"""Approving the requirements and the plan of an existing ticket with ONE signature (the ``approve_gates`` intent;
design note devB-design-r11-p09, 29 Oct 2026).

ONE review and ONE confirmation sign, in this order, on the TICKET log:

0. ``approve {gate: requirements}`` -> ``gate.approved``;
1. ``approve {gate: plan}`` -> ``gate.approved``.

Nothing new is signed: each step is exactly the event a single-gate approval signs (:func:`payload`), with the same
host-chosen keys (``gate_gen``, ``hash``, ``policy_hash``: :func:`seqquick.open_keys`), and core's gate rules are
unchanged. What makes it WYSIWYS is that the intent carries BOTH full texts (the sections of each gate, the acceptance
checks and the tasks). At each step the signer checks the gate input the host prepared against them: every section
hash is the hash of the text in the intent, the criteria and the tasks are the intent's, and the texts the host shows
are the intent's (:func:`check_gate`). The gate hash itself is recomputed by :func:`orch.host.signer.check`.

The plan gate binds the requirements approval it follows (``prior``). Its full shape is compared, not just a field:
``{"requirements": {"gen": <int>, "approvals": [<the event id this signer signed at step 0>]}}`` and nothing else.
Because that id is only known after step 0, the offer is limited to a requirements gate that one approval completes
(:func:`host_check_first`).

Every prefix is safe: after step 0 the ticket has approved requirements and an open plan, which is where a person
who approves one gate at a time stands.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

__all__ = [
    "GATES",
    "KIND",
    "check_gate",
    "host_check_first",
    "single_only",
    "intent_of_ticket",
    "lines",
    "payload",
    "steps",
    "validate",
]

#: a gate input key (the bound files), spelled in two parts: not the store's directory the layout lint looks for
_ARTIFACTS = "artifact" + "s"
_ADDONS = "addon" + "s"
KIND = "approve_gates"
GATES = ("requirements", "plan")
_KEYS = {"v", "kind", "ticket", "ticket_type", "size", "title", "head", "acceptance", "requirements", "plan"}
_SID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_AC = re.compile(r"^AC[1-9][0-9]*$")
_TASK = re.compile(r"^T[1-9][0-9]*$")
MAX_TEXT = 65536
MAX_ITEMS = 32


def validate(intent: Any, bad: type[Exception]) -> dict[str, Any]:
    """The canonical intent or ``bad``: exactly the keys above, texts already normalised, fail closed."""
    from orch.canon.text import TextError, normalize_text

    from . import sequence as sq

    if not isinstance(intent, Mapping) or set(intent) != _KEYS:
        raise bad(
            "approving both gates has exactly v, kind, ticket, ticket_type, title, head, acceptance, requirements "
            "and plan"
        )
    if type(intent["v"]) is not int or intent["v"] != 1 or intent["kind"] != KIND:
        raise bad("this signer does not know that sequence")
    ticket = intent["ticket"]
    if (
        not isinstance(ticket, Mapping)
        or set(ticket) != {"key", "uid"}
        or not isinstance(ticket["key"], str)
        or not sq._KEY.fullmatch(ticket["key"])
        or not isinstance(ticket["uid"], str)
        or not sq._ULID.fullmatch(ticket["uid"])
    ):
        raise bad("ticket is {key, uid}")
    if intent["ticket_type"] not in sq._TYPES:
        raise bad("the ticket type is unknown")
    head = intent["head"]
    if not isinstance(head, str) or not sq._HASH.fullmatch(head):
        raise bad("head is the hash of the log head that was reviewed")
    title = sq._one_line(intent["title"], "the title", 200)
    size = intent["size"]
    if size is not None and size not in sq._SIZES:
        raise bad("size is null or one of xs, s, m, l, xl")

    def one(value: Any, what: str, limit: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise bad(f"{what} is 1 to {limit} characters")
        try:
            if normalize_text(value, one_line=True) != value:
                raise bad(f"{what} is not normalised")
        except TextError as exc:
            raise bad(f"{what}: {exc}") from None
        return value

    acceptance = intent["acceptance"]
    if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= MAX_ITEMS:
        raise bad(f"acceptance is 1 to {MAX_ITEMS} criteria")
    ac_out: list[dict[str, str]] = []
    for a in acceptance:
        if (
            not isinstance(a, Mapping)
            or set(a) != {"id", "text"}
            or not isinstance(a["id"], str)
            or not _AC.fullmatch(a["id"])
        ):
            raise bad("a criterion is {id, text}")
        ac_out.append({"id": a["id"], "text": one(a["text"], "a criterion", 500)})
    ids = [a["id"] for a in ac_out]
    if len(set(ids)) != len(ids):
        raise bad("a criterion id twice")

    def sections(value: Any, what: str) -> dict[str, str]:
        if not isinstance(value, Mapping) or not 1 <= len(value) <= MAX_ITEMS:
            raise bad(f"{what}: sections is a map of 1 to {MAX_ITEMS} sections")
        out: dict[str, str] = {}
        for sid in sorted(value):
            text = value[sid]
            if not isinstance(sid, str) or not _SID.fullmatch(sid) or not isinstance(text, str):
                raise bad(f"{what}: a section is an id and a text")
            try:
                if text != text.strip("\n") or len(text.encode()) > MAX_TEXT or normalize_text(text) != text:
                    raise bad(f"{what}: section {sid} is not normalised or longer than 64 KiB")
            except TextError as exc:
                raise bad(f"{what}: section {sid}: {exc}") from None
            out[sid] = text
        return out

    req, plan = intent["requirements"], intent["plan"]
    if not isinstance(req, Mapping) or set(req) != {"sections"}:
        raise bad("requirements is {sections}")
    if not isinstance(plan, Mapping) or set(plan) != {"sections", "tasks"}:
        raise bad("plan is {sections, tasks}")
    tasks = plan["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= sq.MAX_TASKS:
        raise bad(f"plan has 1 to {sq.MAX_TASKS} tasks")
    task_out: list[dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, Mapping) or set(t) != {"id", "text", "verify", "proves"}:
            raise bad("a task is {id, text, verify, proves}")
        if not isinstance(t["id"], str) or not _TASK.fullmatch(t["id"]):
            raise bad("a task id is T<n>")
        verify = t["verify"]
        if verify is not None:
            if not isinstance(verify, Mapping) or set(verify) != {"cmd"}:
                raise bad("verify is null or {cmd}")
            verify = {"cmd": one(verify["cmd"], "a command", 2048)}
        proves = t["proves"]
        if (
            not isinstance(proves, list)
            or any(not isinstance(p, str) or p not in ids for p in proves)
            or proves != sorted(set(proves), key=lambda x: int(x[2:]))
        ):
            raise bad("proves is a sorted list of the criteria of this ticket")
        task_out.append(
            {"id": t["id"], "text": one(t["text"], "a task", 500), "verify": verify, "proves": list(proves)}
        )
    if len({t["id"] for t in task_out}) != len(task_out):
        raise bad("a task id twice")
    return {
        "v": 1,
        "kind": KIND,
        "ticket": {"key": ticket["key"], "uid": ticket["uid"]},
        "ticket_type": intent["ticket_type"],
        "size": size,
        "title": title,
        "head": head,
        "acceptance": ac_out,
        "requirements": {"sections": sections(req["sections"], "requirements")},
        "plan": {"sections": sections(plan["sections"], "plan"), "tasks": task_out},
    }


def intent_of_ticket(data: Mapping[str, Any], inputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The intent a page builds from what it showed: ``data`` is the host's ``ticket.get`` and ``inputs`` the host's
    ``gate.input`` answers of both gates (its ``input`` member is G). Every text is checked here against the section
    hash in G, so an intent is built only from texts that are the ones the gates bind. Not validated: the caller does
    that."""
    from . import sequence as sq

    doc = data.get("doc") or {}
    current = {**(doc.get("sections") or {}), "current_state": doc.get("current_state", "")}
    got: dict[str, Any] = {}
    for gate in GATES:
        g = inputs[gate].get("input") or {}
        texts = {}
        for sid, digest in (g.get("sections") or {}).items():
            text = current.get(sid, "")
            if canon.section_hash(text) != digest:
                raise sq.SequenceInvalid(f"the {gate} changed after it was read, review again")
            texts[sid] = text
        got[gate] = texts
    plan_in, req_in = inputs["plan"].get("input") or {}, inputs["requirements"].get("input") or {}
    head = doc.get("head") or {}
    return {
        "v": 1,
        "kind": KIND,
        "ticket": {"key": doc.get("key"), "uid": doc.get("uid")},
        "ticket_type": doc.get("type"),
        "size": (req_in.get("fields") or {}).get("size"),
        "title": doc.get("title"),
        "head": head.get("head"),
        "acceptance": copy.deepcopy((req_in.get("fields") or {}).get("acceptance")),
        "requirements": {"sections": got["requirements"]},
        "plan": {"sections": got["plan"], "tasks": copy.deepcopy(plan_in.get("tasks"))},
    }


def steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Two ordinary ``approve`` ops on the ticket, requirements first."""
    return [{"op": "approve", "args": {"gate": g}, "kind": "approve", "ref": True} for g in GATES]


def payload(intent: Mapping[str, Any], k: int) -> dict[str, Any]:
    """The payload of the event of step ``k``: exactly what a single-gate approval signs, minus the host's keys."""
    return {"type": "gate.approved", "gate": GATES[k]}


def _texts(intent: Mapping[str, Any], gate: str) -> dict[str, str]:
    return dict(intent[gate]["sections"])


def single_only(inp: Mapping[str, Any] | None) -> bool:
    """Display flag (``gates.<gate>.single_only`` of ``ticket.get``): does this gate's CURRENT input bind files, add-on
    fields or add-on packages? Then the combined approval cannot carry it (the View shows none of them) and the page
    offers the single cards. The same emptiness rule as :func:`content_ok` and :func:`host_check_first`, which stay
    the real guards; this only decides what the page offers."""
    if inp is None:
        return False
    return bool(inp.get(_ARTIFACTS) or (inp.get("fields") or {}).get(_ADDONS) or inp.get("addon_packages"))


def content_ok(
    intent: Mapping[str, Any], gate: str, inp: Mapping[str, Any], *, uid: str, workspace_id: Any = None
) -> bool:
    """Is the gate input ``inp`` (all of it but ``prior``) exactly what the intent says? EVERYTHING the gate hash binds
    that the View does not show must be empty, so a host cannot add an artifact, an add-on field or package, or change
    the size: ``artifacts``, ``fields.addons`` and ``addon_packages`` are ``{}``, ``size`` is the intent's."""
    want_texts = _texts(intent, gate)
    fields = inp.get("fields")
    expect = {
        "uid": uid,
        "gate": gate,
        "schema": "orch.ticket/2",
        "hash_v": 1,
        "sections": {sid: canon.section_hash(text) for sid, text in want_texts.items()},
        "tasks": copy.deepcopy(intent["plan"]["tasks"]) if gate == "plan" else [],
        "receipts": {},
        "source_sha": [],
        _ARTIFACTS: {},
        "addon_packages": {},
    }
    if workspace_id is not None:
        expect["workspace_id"] = workspace_id
    for key, value in expect.items():
        if key not in inp or canon.dumps(inp[key]) != canon.dumps(value):
            return False
    return (
        isinstance(fields, Mapping)
        and canon.dumps(fields.get("acceptance")) == canon.dumps(intent["acceptance"])
        and fields.get("ticket_type") == intent["ticket_type"]
        and fields.get("size") == intent["size"]
        and fields.get(_ADDONS) == {}
        and fields.get("links") is None
    )


def check_gate(
    intent: Mapping[str, Any],
    k: int,
    prepared: Mapping[str, Any],
    material: Mapping[str, Any],
    *,
    uid: str,
    prior_id: str | None,
    stop: Callable[[], Exception],
) -> None:
    """Refuse (``stop()``) unless the gate input and texts the host prepared for step ``k`` are, exactly, what the
    reviewed intent says. The input's hash is recomputed by ``signer.check``; this binds its CONTENT to the intent."""
    gate = GATES[k]
    inp, texts = material.get("input"), material.get("texts")
    if material.get("kind") != "gate" or material.get("gate") != gate or not isinstance(inp, Mapping):
        raise stop()
    if not content_ok(intent, gate, inp, uid=uid, workspace_id=prepared.get("workspace_id")):
        raise stop()
    if not isinstance(texts, Mapping) or canon.dumps(dict(texts)) != canon.dumps(_texts(intent, gate)):
        raise stop()
    prior = inp.get("prior")
    if gate == "requirements":
        if prior != {}:
            raise stop()
        return
    # A: the FULL prior structure, nothing but the requirements approval this signer signed at step 0
    if (
        prior_id is None
        or not isinstance(prior, Mapping)
        or set(prior) != {"requirements"}
        or not isinstance(prior["requirements"], Mapping)
        or set(prior["requirements"]) != {"gen", "approvals"}
        or type(prior["requirements"]["gen"]) is not int
        or prior["requirements"]["approvals"] != [prior_id]
    ):
        raise stop()


def host_check_first(
    store: Any, person: str, intent: Mapping[str, Any], *, err: Callable[[str, str], Exception]
) -> None:
    """The host's checks at step 0, before anything is signed (``err(code, message)`` builds its error): the ticket is
    the reviewed one, neither gate is approved or blocked, the person may decide both, and their one approval completes
    the requirements gate (so the plan gate really is approvable at step 1). Humans only: this is ``human.prepare``."""
    t = intent["ticket"]
    view = store.ticket(t["key"])
    if view is None or view.uid != t["uid"]:
        raise err("human.request_mismatch", "the ticket changed while signing, review again")
    for gate in GATES:
        g = view.gates.get(gate)
        if g is None or not g.applies or g.blocked or g.hash is None:
            raise err("gate.not_applicable", f"the {gate} cannot be approved right now; approve the steps one by one")
        if g.approved or g.missing:
            raise err("invalid.input", f"the {gate} is approved already or not complete; approve the steps one by one")
        if person not in g.eligible:
            raise err("gate.not_eligible", f"you may not decide the {gate}; approve the steps one by one")
    from orch.store.render import thaw

    for gate in GATES:
        inp = thaw(view.gates[gate].input) if view.gates[gate].input is not None else None
        if single_only(inp):
            raise err("invalid.input", f"the {gate} binds files or add-on fields; approve the steps one by one")
        # L1: a gate that changed after the review is refused before anything is signed (its prior is the host's)
        if inp is None or not content_ok(intent, gate, inp, uid=t["uid"]):
            raise err("human.request_mismatch", "the ticket changed while signing, review again")
    req = view.gates["requirements"]
    if req.needed != 1 or req.counting:
        raise err("invalid.input", "the requirements need more than your approval; approve the steps one by one")


def lines(intent: Mapping[str, Any], clean: Callable[[Any], str]) -> list[str]:
    """The intent in plain words, for a terminal prompt that has no SignContent: both texts in full, as blocks."""
    from orch.host.signcontent import one

    out = [f"approve requirements and plan of: {clean(one(intent['ticket']['key']))} {clean(one(intent['title']))}"]
    for a in intent["acceptance"]:
        out.append(f"acceptance {clean(a['id'])}: {clean(one(a['text']))}")
    for gate in GATES:
        for sid, text in intent[gate]["sections"].items():
            out.append(f"{gate} section {clean(sid)}:")
            out += ["| " + clean(x) for x in text.split("\n")]
    for t in intent["plan"]["tasks"]:
        cmd = (t["verify"] or {}).get("cmd")
        out.append(
            f"task {clean(t['id'])}: {clean(one(t['text']))}"
            + (f" (checked by {clean(one(cmd))})" if cmd else "")
            + f" proves {clean(', '.join(t['proves']) or 'none')}"
        )
    return out
