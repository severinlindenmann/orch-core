"""DEV ONLY, never shipped: the stub signer, `python -m devhost sign CODE`.

It plays `orch sign CODE` (H5b) against the dev stub host until the real command exists:

1. resolve the code with `human.pending`, fetch and bind with `human.get` (never `human.prepare`);
2. check the shape and recompute every hash, the review, `review_digest` and the 12-hex fingerprint from `material`;
   the host's values are only compared, never trusted;
3. render its own review on the terminal from `material` and the events, ask for the confirmation word;
4. sign with the dev workspace's P1 device custody (the passphrase backend, asked on /dev/tty) and `human.submit`.

Not checked here: git state for a code gate, and the op-specific `ticket.updated` limits of the real signer.

This file lives under `devhost/`, which the wheel excludes. It is a dev aid, not a signing client to ship.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity import device_ref, new_ulid, sign_person_event
from orch.ops.errors import OrchError

from orch_dashboard.errors import HostError
from orch_dashboard.gateway import read_pins
from orch_dashboard.hostlink import HostLink

from . import review

KEY_ID = "dk"
OP_WORDS = {
    "approve": "approve a gate of",
    "request_changes": "ask for changes on",
    "verdict": "give the verdict on",
    "answer": "answer a question on",
    "close": "close",
    "reopen": "reopen",
    "log": "add a comment to",
    "set": "change fields of",
    "section.set": "write a section of",
    "new": "create the ticket",
    "settings.set": "change the workspace settings",
    "policy.set": "change the default policy of a gate",
    "member.add": "add a member to the workspace",
    "member.role": "change the role of a member",
    "member.remove": "remove a member from the workspace",
    "grant.revoke": "revoke a grant",
}
WORKSPACE_OPS = ("settings.set", "policy.set", "member.add", "member.role", "member.remove", "grant.revoke")
MEMBER_OPS = ("member.add", "member.role", "member.remove")
WARNING = "only sign a code you created yourself"


class Refused(Exception):
    """The prepared request is not what a signer may sign: nothing is shown or signed."""


def clean(value: Any) -> str:
    text = value if isinstance(value, str) else review.cj(value).decode()
    return canon.clean(text).replace("\n", "⟨U+000A⟩")


def data(text: str) -> list[str]:
    """Text written by people or agents: escaped, one `| ` mark per line, so it cannot pass for a heading."""
    return ["| " + canon.clean(line) for line in text.split("\n")]


def check(prepared: dict[str, Any], *, workspace_id: str, person: str, device: str, auth: str) -> None:
    """The signer's own checks (v1.4 0.8.1 'What the signer does', step 1). Raises `Refused`, whatever is malformed."""
    try:
        recompute(prepared, workspace_id=workspace_id, person=person, device=device, auth=auth)
    except (canon.HashError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise Refused(f"the request is malformed ({type(exc).__name__})") from None


def recompute(prepared: dict[str, Any], *, workspace_id: str, person: str, device: str, auth: str) -> None:
    events, material, op = prepared.get("events"), prepared.get("material"), prepared.get("op")
    if prepared.get("workspace_id") != workspace_id:
        raise Refused("the request names another workspace")
    if op not in review.EMITS:
        raise Refused(f"{clean(op)} is not an operation this signer knows")
    if not isinstance(events, list) or not 1 <= len(events) <= 16 or not isinstance(material, dict):
        raise Refused("the request has no 1 to 16 events and a material")
    if len(events) != 1:
        raise Refused("every operation of this signer is one event")
    bases = {e.get("based_on") for e in events}
    if len({e.get("id") for e in events}) != len(events):
        raise Refused("two events share an id")
    for e in events:
        if e.get("type") not in review.EMITS[op]:
            raise Refused(f"event type {clean(e.get('type'))} is not one of {op}")
        actor = e.get("actor") or {}
        if actor.get("kind") != "person" or actor.get("id") != person:
            raise Refused("an event names another person")
        if actor.get("device") != device or e.get("auth") != auth:
            raise Refused("an event is not bound to this device")
        if "sig" in e:
            raise Refused("an event is already signed")
    if len(bases) != 1:
        raise Refused("the events do not share one based_on")
    # Bound hashes, recomputed from the material that is shown.
    ticket = material.get("ticket") or {}
    for e in events:
        if e["type"] in ("gate.approved", "gate.changes_requested", "verdict.given"):
            if material.get("kind") != "gate" or e.get("hash") != canon.gate_hash(material["input"]):
                raise Refused("the gate hash is not the hash of the content shown")
        if e["type"] == "question.answered":
            q = material.get("question") or {}
            qid = canon.question_id(workspace_id, ticket["uid"], q["id"])
            if e.get("hash") != canon.question_hash(qid, ticket["uid"], q):
                raise Refused("the question hash is not the hash of the question shown")
        if op in WORKSPACE_OPS:
            if prepared.get("log") != "workspace" or material.get("ticket") is not None:
                raise Refused("a workspace operation is signed in the workspace log, with no ticket")
            if op in MEMBER_OPS and not canon_person(e.get("person")):
                raise Refused("the member is not a person id")
        elif prepared.get("log") != ticket.get("uid"):
            raise Refused("the log is not the ticket shown")  # one log per request: the ticket's own
        if "source_sha" in e and (material.get("input") or {}).get("source_sha") not in (None, e["source_sha"]):
            raise Refused("source_sha is not the one the material shows")
        if e["type"] == "ticket.updated":
            sets_, sections_ = e.get("set") or {}, e.get("sections") or {}
            if op == "section.set" and (sets_ or len(sections_) != 1):
                raise Refused("section.set changes one section and no field")
            if op == "set" and (sections_ or not sets_):
                raise Refused("set changes fields only")
            if set(e.get("base_rev") or {}) != set(sets_) | {"body." + sid for sid in sections_}:
                raise Refused("base_rev does not name exactly the paths the edit touches")
            before = material.get("before") or {}
            if set(before) - set(e.get("set") or {}) - {"body." + sid for sid in e.get("sections") or {}}:
                raise Refused("a 'before' value for a path the event does not change")
            for path, value in before.items():
                want = canon.section_hash(value) if path.startswith("body.") else canon.value_hash(value)
                if (e.get("base_rev") or {}).get(path) != want:
                    raise Refused(f"the current value of {clean(path)} does not match base_rev")
        for sid, entry in (review.derived_of(e, material).get("sections") or {}).items():
            if entry["text"] is None or canon.section_hash(entry["text"]) != entry["hash"]:
                raise Refused(f"section {clean(sid)} does not match its hash")
    if not review.material_keys_ok(material):
        raise Refused(f"material has keys outside the {clean(material.get('kind'))} schema")
    if material.get("kind") == "gate":
        gate = material["gate"]
        for e in events:
            if e.get("policy_hash") != canon.policy_hash(gate, material["policy"]):
                raise Refused("the policy hash is not the hash of the policy shown")
        for sid, h in (material["input"].get("sections") or {}).items():
            if canon.section_hash(material["texts"].get(sid, "")) != h:
                raise Refused(f"section {clean(sid)} does not match the hash the gate binds")
    if material.get("review") != review.reviews_of(events, material):
        raise Refused("the review of the host is not the review computed here")
    digest = review.digest_of(material)
    if prepared.get("review_digest") != digest:
        raise Refused("review_digest differs from the one recomputed")
    if prepared.get("fingerprint") != review.fingerprint(digest, events, op):
        raise Refused("the fingerprint differs from the one recomputed")


def canon_person(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 34 and value.startswith("p_")


def confirm_word(prepared: dict[str, Any], key_word: str | None, person: str, prefix: str = "") -> str:
    """Ticket ops: KEY/FRAG. Member ops: the member's person id. Other workspace ops: the op name in capitals."""
    if key_word:
        return key_word
    if prepared["op"] in MEMBER_OPS:
        return clean(prepared["events"][0].get("person"))
    if prepared["op"] in ("settings.set", "policy.set"):
        # stream A: <PREFIX>~<WS8>/SETTINGS, WS8 the last 8 characters of the workspace id (the header has the full one)
        return (
            f"{prefix}~{prepared['workspace_id'][-8:]}/{'SETTINGS' if prepared['op'] == 'settings.set' else 'POLICY'}"
        )
    return prepared["op"].upper() if prepared["op"] in WORKSPACE_OPS else person


def render(prepared: dict[str, Any], *, workspace: dict[str, Any], person: str) -> tuple[str, str]:
    """(text, confirmation word). The header comes first, from the signer's own registry and the checked data."""
    events, material = prepared["events"], prepared["material"]
    ticket = material.get("ticket") or {}
    origin = prepared.get("origin") or {}
    frag = clean((prepared.get("log") or "")[-8:])
    key = clean(ticket["key"]) if ticket.get("key") else None
    words = OP_WORDS.get(prepared["op"], prepared["op"])
    on = f" {key}/{frag}" if key else ""
    out = [
        f"workspace: {clean(workspace['name'])} ({clean(workspace['prefix'])}) {clean(prepared['workspace_id'])}"
        "  [from your own registry]",
        f"op: {clean(prepared['op'])}: {words}{on}",
    ]
    if key:
        out.append(
            f"ticket: {key} (ticket key per host, not proven) uid {clean(prepared['log'])}"
            f" (signed; you type its last eight: {frag})"
        )
    out += [
        f"requested by uid {clean(origin.get('uid'))} pid {clean(origin.get('pid'))}"
        f" at {clean(origin.get('created_at'))}; client {clean(origin.get('client'))} (self-declared, unverified)",
        f"fingerprint: {prepared['fingerprint']}  (the dashboard shows the same)",
        WARNING,
        "",
    ]
    reviews = material["review"]
    for n, r in enumerate(reviews, 1):
        out.append(f"--- event {n} of {len(reviews)}: {clean(r['type'])} ---")
        for name in sorted(r["payload"]):
            value = r["payload"][name]
            if isinstance(value, str) and name in ("text", "title"):
                out += [f"{clean(name)}:", *data(value)]
            else:
                out.append(f"{clean(name)}: {clean(value)}")
        for name in sorted(r["derived"]):
            out.append(f"derived (stated by the host) {clean(name)}: {clean(r['derived'][name])}")
    if material.get("before"):
        out.append("current values according to the host, not signed:")
        out += [f"  {clean(k)}: {clean(v)}" for k, v in sorted(material["before"].items())]
    if material.get("kind") == "gate":
        out += ["", f"gate: {clean(material['gate'])}", f"gate hash {events[0].get('hash', '')[7:19]}"]
    return "\n".join(out) + "\n", confirm_word(
        prepared, f"{key}/{frag}" if key else None, person, clean(workspace["prefix"])
    )


def run(
    code: str,
    *,
    state: Path,
    run_dir: Path,
    open_backend: Callable[[Path], Any],
    confirm: Callable[[str, str], bool],
    out: Callable[[str], None] = print,
    connect: Callable[..., HostLink] = HostLink.connect,
) -> int:
    from orch.ops.human import keys_dir

    pins = read_pins(state / "host/workspaces.json")
    link = request = workspace_id = None
    for wid, pin in pins.items():
        candidate = connect(run_dir / wid / "host.sock", pin["host_uid"], wid)
        found = candidate.call("human.pending", {"code": code})["requests"]
        if found:
            link, request, workspace_id, root = candidate, found[0], wid, Path(pin["root"])
            break
        candidate.close()
    if link is None or request["state"] not in ("prepared", "bound"):
        out(f"no open request for {code}\n")
        return 1
    try:
        person = link.welcome["peer"]["person"]
        config = canon.loads_strict((root / ".orch" / "config.json").read_bytes())["workspace"]
        backend = open_backend(keys_dir(state, workspace_id))
        device = device_ref(backend.public_key(KEY_ID))
        prepared = link.call(
            "human.get", {"request_id": request["id"], "signer": {"device": device, "auth": backend.auth}}
        )
        check(prepared, workspace_id=workspace_id, person=person, device=device, auth=backend.auth)
        text, word = render(
            prepared, workspace={"name": config.get("name", ""), "prefix": config["prefix"]}, person=person
        )
        if not confirm(text, word):
            out("not confirmed: nothing was signed\n")
            return 1
        signed = []
        for event in prepared["events"]:
            ev = dict(event)
            ev["sig"] = sign_person_event(
                backend, KEY_ID, workspace_id, prepared["log"], ev, action=f"{prepared['op']} {word}"
            )
            signed.append(ev)
        done = link.call(
            "human.submit",
            {"request_id": request["id"], "events": signed},
            idem="a_" + new_ulid(),
        )
        out(f"signed: {prepared['op']} {word} (fingerprint {prepared['fingerprint']}) {done}\n")
        return 0
    except Refused as exc:
        out(f"refused: {exc}; nothing was signed\n")
        return 2
    except (HostError, OrchError) as exc:
        code_ = exc.error["code"] if isinstance(exc, HostError) else exc.code
        out(f"failed: {code_}\n")
        return 1
    finally:
        link.close()


def main(code: str) -> int:
    from orch.ops.human import open_backend, review_prompt

    from .__main__ import dev_directories

    if os.environ.get("ORCH_DEV") != "1":
        raise RuntimeError("the stub signer requires ORCH_DEV=1 (never shipped)")
    state, run_dir = dev_directories()
    return run(
        code,
        state=state,
        run_dir=run_dir,
        open_backend=open_backend,
        confirm=lambda text, word: review_prompt(text, word),
        out=lambda text: sys.stdout.write(text),
    )
