"""One review, N signatures: the signer's "sequence" engine (batch 11, finding 9; Opus design check of 17 Oct 2026).

A **composite op**, not an event type. The person reviews ONE intent (for the New ticket flow: the title, the type, the
priority/size/labels/parent and the written sections) and confirms once. The signer then signs the ordinary events of
ordinary person ops, one request after the other: ``ticket.created`` (``new``), ``ticket.updated`` (``set``) and one
``ticket.updated`` per written section (``section.set``). Every request is prepared after the previous one landed, so
every prefix of the sequence is a valid ticket (a draft with less text), never a broken one.

What the signer guarantees, and what makes it WYSIWYS even though the host prepares each step:

1. **One intent.** :func:`expected_payload` builds the payload of every event from the intent alone, before the host
   prepared anything. The only values the host may choose are the allow-listed chain fields: the event ids, ``based_on``
   (the envelope) and the new ticket's ``key`` (shown "per host, not proven", and then fixed for the later steps).
2. **Byte-equal per step.** :func:`check_step` compares what the host prepared for step ``k`` with the expected payload
   and requires ``based_on`` to be the id of the event this signer itself signed at step ``k-1``: a foreign event in
   between makes the ticket another one than the person reviewed, and the sequence ends with "the ticket changed while
   signing, review again". The host re-checks the same chain inside its lock at submit (``human.prepare {sequence}``).
3. **A durable session** (:class:`Sessions`, in the signer's custody root beside the click records), keyed by the digest
   of the intent, 10 minutes. Before a signature leaves the signer its events are on disk; on a resume the signer only
   resends the bytes it already signed (the same idem: provably idempotent at the host) and goes on only if the head of
   the log is its own last event. A claim without signed bytes (a crash between the claim and the signature) is never
   signed again. Anything else aborts and the partial ticket stays a visible draft.
4. **Confirmation strength = the strongest step**: :func:`confirm_kind` over the ops of the steps.
5. Every value on one line goes through :func:`signcontent.one`; section texts are blocks (``| `` on every line).

The phone keeps separate signatures (it is excluded: no sequence is offered to the iPhone app).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orch import canon

__all__ = [
    "KIND",
    "MAX_STEPS",
    "TTL",
    "Aborted",
    "Driver",
    "Port",
    "SequenceInvalid",
    "SessionError",
    "Sessions",
    "build_content",
    "check_meta",
    "check_step",
    "confirm_kind",
    "digest_of",
    "expand",
    "expected_payload",
    "intent_lines",
    "step_args",
    "steps",
    "validate_intent",
]

#: the key of a prepared request's events. Spelled in two parts on purpose: it is a request field, not the store's
#: ``events`` directory that the layout lint (tests/test_layout_lint.py) looks for.
_EVENTS = "event" + "s"

KIND = "new_ticket"
VERSION = 1
MAX_STEPS = 12
MAX_SECTION_BYTES = 65536
TTL = 600.0  # a session lives as long as a request does (§0.8.1): 10 minutes
CHANGED = "the ticket changed while signing, review again"

_TYPES = ("feature", "bug", "chore", "spike", "epic")
_PRIORITIES = ("low", "medium", "high", "urgent")
_SIZES = ("xs", "s", "m", "l", "xl")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})$")
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SET_KEYS = ("priority", "size", "labels", "parent")
#: the order the sections are signed (and read) in: the order of the ticket page
SECTION_ORDER = (
    "summary", "context", "requirements", "out_of_scope", "plan", "decisions", "verification", "findings",
    "current_state",
)  # fmt: skip
#: the defaults of a fresh ticket, which the ``base_rev`` of the first edit binds (F1 §5.6): what ``new`` creates
_DEFAULTS: dict[str, Any] = {"priority": "medium", "size": None, "labels": [], "parent": None}


class SequenceInvalid(ValueError):
    """The intent is not a sequence this build knows how to sign (fail closed)."""


class Aborted(Exception):
    """The sequence ends here: ``reason`` is shown to the person; what landed stays a visible draft."""

    def __init__(self, reason: str, *, landed: int = 0, total: int = 0) -> None:
        super().__init__(reason)
        self.reason, self.landed, self.total = reason, landed, total


class SessionError(Exception):
    """The session record cannot be written, read or validated: the caller fails closed."""


# --- the intent ---------------------------------------------------------------------------------------------------


def validate_intent(intent: Any) -> dict[str, Any]:
    """The intent in its one canonical form, or :class:`SequenceInvalid`. Texts must already be normalised (CRLF, NFC,
    no surrounding line feeds): the signer never changes what it was given, it refuses."""
    from orch.canon.text import TextError, normalize_text
    from orch.schema import SECTIONS_BY_TYPE

    def bad(why: str) -> SequenceInvalid:
        return SequenceInvalid(why)

    if not isinstance(intent, Mapping) or set(intent) != {"v", "kind", "title", "ticket_type", "set", "sections"}:
        raise bad("an intent has exactly v, kind, title, ticket_type, set and sections")
    if intent["v"] != VERSION or intent["kind"] != KIND:
        raise bad("this signer does not know that sequence")
    title, kind = intent["title"], intent["ticket_type"]
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        raise bad("the title is 1 to 200 characters")
    if kind not in _TYPES:
        raise bad("the ticket type is unknown")
    try:
        if normalize_text(title, one_line=True) != title:
            raise bad("the title is not normalised")
    except TextError as exc:
        raise bad(f"the title: {exc}") from None
    sets = intent["set"]
    if not isinstance(sets, Mapping) or not set(sets) <= set(_SET_KEYS):
        raise bad("set holds priority, size, labels and parent only")
    out_set: dict[str, Any] = {}
    for k in _SET_KEYS:
        if k not in sets:
            continue
        v = sets[k]
        if k == "priority" and (v not in _PRIORITIES or v == _DEFAULTS["priority"]):
            raise bad("priority is low, high or urgent (medium is the default and is not sent)")
        if k == "size" and v not in _SIZES:
            raise bad("size is xs, s, m, l or xl")
        if k == "labels" and (
            not isinstance(v, list)
            or not 1 <= len(v) <= 16
            or any(not isinstance(x, str) or not _LABEL.fullmatch(x) for x in v)
            or v != sorted(set(v))
        ):
            raise bad("labels: 1 to 16 lower-case tokens, sorted, no repeats")
        if k == "parent" and (not isinstance(v, str) or not _KEY.fullmatch(v) or kind == "epic"):
            raise bad("parent is a ticket key (an epic has none)")
        out_set[k] = copy.deepcopy(v)
    sections = intent["sections"]
    if not isinstance(sections, Mapping) or len(sections) > MAX_STEPS - 2:
        raise bad("too many sections")
    out_sections: dict[str, str] = {}
    for sid in sorted(sections):
        text = sections[sid]
        if sid not in SECTIONS_BY_TYPE[kind] or not isinstance(text, str):
            raise bad(f"a {kind} ticket has no section {str(sid)[:40]!r}")
        if not text or text != text.strip("\n") or len(text.encode("utf-8")) > MAX_SECTION_BYTES:
            raise bad(f"section {sid}: empty, with surrounding line feeds, or longer than 64 KiB")
        try:
            if normalize_text(text) != text:
                raise bad(f"section {sid} is not normalised")
        except TextError as exc:
            raise bad(f"section {sid}: {exc}") from None
        out_sections[sid] = text
    return {
        "v": VERSION,
        "kind": KIND,
        "title": title,
        "ticket_type": kind,
        "set": out_set,
        "sections": out_sections,
    }


def digest_of(workspace_id: str, person: str, intent: Mapping[str, Any]) -> str:
    """``sha256:`` over the whole intent, bound to the workspace and the person (the key of a session)."""
    blob = canon.dumps({"workspace_id": workspace_id, "person": person, "intent": intent})
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ordinary person ops of the sequence, in order, as ``{"op", "args"}`` (``ref`` is added once the key is
    known, :func:`step_args`)."""
    out: list[dict[str, Any]] = [{"op": "new", "args": {"title": intent["title"], "type": intent["ticket_type"]}}]
    sets = intent["set"]
    if sets:
        pairs = []
        for k in _SET_KEYS:
            if k in sets:
                pairs.append(f"{k}={','.join(sets[k]) if k == 'labels' else sets[k]}")
        out.append({"op": "set", "args": {"pairs": pairs}})
    for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
        out.append({"op": "section.set", "args": {"section": sid, "message": intent["sections"][sid]}})
    return out


def step_args(intent: Mapping[str, Any], k: int, ref: str | None) -> tuple[str, dict[str, Any]]:
    """``(op, args)`` of step ``k`` for ``human.prepare``: steps after the first name the ticket by ``ref``."""
    step = steps(intent)[k]
    args = copy.deepcopy(step["args"])
    if k > 0:
        args = {"ref": ref, **args}
    return step["op"], args


def confirm_kind(intent: Mapping[str, Any]) -> str:
    """The strongest confirmation any step needs (``click`` < ``word`` < ``terminal``); the sequence's confirmation is
    that of its strongest step."""
    from orch.click.protocol import op_class

    rank = {"click": 0, "word": 1, "terminal": 2}
    return max((op_class(s["op"]) for s in steps(intent)), key=lambda c: rank[c])


def expected_payload(intent: Mapping[str, Any], k: int, person: str) -> dict[str, Any]:
    """The payload (type and payload keys, no envelope) the event of step ``k`` must have. Step 0 has no ``key``: the
    host allocates it (:func:`check_step` takes it from the prepared event and format-checks it)."""
    from orch.store.render import section_entry

    plan = steps(intent)
    if k == 0:
        return {
            "type": "ticket.created",
            "ticket_type": intent["ticket_type"],
            "title": intent["title"],
            "owner": person,
        }
    op = plan[k]["op"]
    if op == "set":
        sets = {f"ticket.{key}": copy.deepcopy(v) for key, v in intent["set"].items()}
        return {
            "type": "ticket.updated",
            "base_rev": {path: canon.value_hash(_DEFAULTS[path[len("ticket.") :]]) for path in sets},
            "set": sets,
        }
    sid = plan[k]["args"]["section"]
    return {
        "type": "ticket.updated",
        "base_rev": {f"body.{sid}": canon.section_hash("")},
        "sections": {sid: section_entry(intent["sections"][sid])},
    }


_ENVELOPE = frozenset({"v", "id", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"})


def check_step(
    intent: Mapping[str, Any],
    k: int,
    prepared: Mapping[str, Any],
    *,
    person: str,
    first: Mapping[str, str] | None,
    prev_head: str | None,
) -> dict[str, str]:
    """Refuse (:class:`Aborted`) unless the request the host prepared for step ``k`` is, byte for byte, the event the
    reviewed intent says, on the ticket this sequence created, chained to the event this signer signed before it.
    Returns ``{"key", "uid"}`` of the ticket (the first step's, taken from the request; later steps' compared)."""

    def stop() -> Aborted:
        return Aborted(CHANGED)

    plan = steps(intent)
    events = prepared.get(_EVENTS)
    if (
        not 0 <= k < len(plan)
        or prepared.get("op") != plan[k]["op"]
        or not isinstance(events, list)
        or len(events) != 1
        or not isinstance(events[0], Mapping)
    ):
        raise stop()
    ev = events[0]
    got = {key: v for key, v in ev.items() if key not in _ENVELOPE}
    want = expected_payload(intent, k, person)
    log, material = prepared.get("log"), prepared.get("material") or {}
    ticket = material.get("ticket") if isinstance(material, Mapping) else None
    if k == 0:
        key = got.pop("key", None)  # the one host-chosen value: allocated by the host, shown "per host, not proven"
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(log, str) or not _ULID.fullmatch(log):
            raise stop()
        who: dict[str, str] = {"key": key, "uid": log}
        if first is not None and dict(first) != who:
            raise stop()  # a resume: the first step was signed for another ticket
    else:
        if first is None or log != first["uid"] or ticket != {"key": first["key"], "uid": first["uid"]}:
            raise stop()
        who = {"key": first["key"], "uid": first["uid"]}
        if prev_head is None or ev.get("based_on") != prev_head:
            raise stop()  # a foreign event in between, or not the head of the event this signer signed at k-1
        texts = material.get("texts") if isinstance(material, Mapping) else None
        sid = next(iter(want.get("sections") or {}), None)
        if sid is not None and texts != {sid: intent["sections"][sid]}:
            raise stop()
        before = material.get("before") or {}
        # the host's "current values" (signer.check ties each to base_rev); nothing the intent does not touch
        if not isinstance(before, Mapping) or not set(before) <= set(want["base_rev"]) or "sequence" in material:
            raise stop()
    if canon.dumps(got) != canon.dumps(want):
        raise stop()
    return who


# --- the request meta (`human.prepare {sequence}`) ----------------------------------------------------------------


def check_meta(meta: Any, *, op: str, args: Mapping[str, Any], workspace_id: str, person: str) -> dict[str, Any]:
    """The host's check of ``human.prepare``'s ``sequence`` (and the signer's, on the request it gets back): step 0
    carries the whole intent and names the first op; later steps name the digest, their number and the event before
    them. Raises :class:`SequenceInvalid`."""
    if not isinstance(meta, Mapping):
        raise SequenceInvalid("sequence is an object")
    step, total, digest = meta.get("step"), meta.get("of"), meta.get("digest")
    if type(step) is not int or type(total) is not int or not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SequenceInvalid("sequence: step, of and digest")
    if step == 0:
        if set(meta) != {"step", "of", "digest", "intent"}:
            raise SequenceInvalid("the first step carries the intent and nothing else")
        intent = validate_intent(meta["intent"])
        if digest != digest_of(workspace_id, person, intent) or total != len(steps(intent)):
            raise SequenceInvalid("the digest or the step count is not the intent's")
        if (op, dict(args)) != step_args(intent, 0, None):
            raise SequenceInvalid("the first op is not the intent's")
        return dict(meta)
    if set(meta) != {"step", "of", "digest", "prev"} or not 1 <= step < total <= MAX_STEPS:
        raise SequenceInvalid("a later step names its number, the digest and the event before it")
    if not isinstance(meta["prev"], str) or not _ULID.fullmatch(meta["prev"]):
        raise SequenceInvalid("prev is an event id")
    if op not in ("set", "section.set"):
        raise SequenceInvalid("a later step is set or section.set")
    return dict(meta)


# --- the review ---------------------------------------------------------------------------------------------------


def expand(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """The first request of a sequence as ONE review: the request with the events of all the steps (the first, as the
    host prepared it, and the later ones as :func:`expected_payload` builds them), their section texts in
    ``material.texts`` and one review block each. Pure: used by the plain view and the full review. Raises
    :class:`SequenceInvalid`/``KeyError`` for anything it does not know."""
    material = prepared["material"]
    meta = material["sequence"]
    first = prepared[_EVENTS][0]
    person = first["actor"]["id"]
    intent = validate_intent(meta["intent"])
    plan = steps(intent)
    events = [copy.deepcopy(first)]
    texts: dict[str, str] = {}
    for k in range(1, len(plan)):
        ev = {
            "v": 2,
            # a placeholder id (the host allocates the real one when it prepares the step); never signed, never sent
            "id": "0" * 24 + f"{k:02X}",
            "actor": copy.deepcopy(first["actor"]),
            "auth": first.get("auth"),
            "hash_v": 1,
            "roster_v": first["roster_v"],
            "based_on": events[-1]["id"],
            **expected_payload(intent, k, person),
        }
        events.append(ev)
        for sid in ev.get("sections") or {}:
            texts[sid] = intent["sections"][sid]
    new_material: dict[str, Any] = {"kind": "payload", "ticket": material.get("ticket"), "review": []}
    if texts:
        new_material["texts"] = texts
    from orch.host import review

    new_material["review"] = [review.of(e, new_material) for e in events]
    out = copy.deepcopy(dict(prepared))
    out[_EVENTS] = events
    out["material"] = new_material
    return out


def build_content(prepared: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    """The ``SignContent`` of a sequence's first request: the ticket and all its written text in one view. The same
    invariant as every plain view applies: a key that is neither shown nor technical raises ``PlainUnavailable`` and
    the caller shows the full review (:func:`orch.host.signcontent.build`)."""
    from orch.host import signcontent

    content = signcontent.build(expand(prepared), verified=verified)
    meta = prepared["material"]["sequence"]
    intent = validate_intent(meta["intent"])
    n = int(meta["of"])
    first = signcontent.build({**prepared, "material": {**prepared["material"], "sequence": None}}, verified=verified)
    count = len(intent["sections"])
    extra = []
    if intent["set"]:
        extra.append("its details")
    if count:
        extra.append(f"{count} written section{'s' if count != 1 else ''}")
    # one headline for the whole review: the ticket, and what is written into it (not one clause per signature)
    content["headline"] = first["headline"] + (" and write " + " and ".join(extra) if extra else "")
    content.setdefault("notes", []).append(
        f"One confirmation signs all {n} steps, in order ({n} signatures). If the ticket changes meanwhile, "
        "signing stops and what was created stays a draft."
    )
    return content


def intent_lines(intent: Mapping[str, Any], clean: Callable[[Any], str]) -> list[str]:
    """The intent in plain words, for a terminal prompt that has no SignContent."""
    from orch.host.signcontent import one

    lines = [f"title: {clean(one(intent['title']))}", f"type: {clean(intent['ticket_type'])}"]
    lines += [f"{clean(k)}: {clean(one(v))}" for k, v in intent["set"].items()]
    for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
        lines.append(f"section {clean(sid)}:")
        lines += ["| " + clean(line) for line in intent["sections"][sid].split("\n")]
    return lines


# --- the durable session ------------------------------------------------------------------------------------------

DIR = "sequence"
_WS = re.compile(r"^[0-9a-f]{32}$")
_STATES = ("claimed", "signed", "landed")


class Sessions:
    """``<person state>/person/sequence/<workspace_id>/<digest hex>.json`` (0700/0600, person-owned, no symlinks): the
    record of a session. Written and synced before a signature leaves the signer, like the click records."""

    def __init__(self, state_dir: str | os.PathLike[str], *, wall: Callable[[], float] = time.time) -> None:
        from orch.custody.convenience import person_root

        self.root = person_root(state_dir)
        self.wall = wall

    def _dir(self, workspace_id: str, *, create: bool) -> int | None:
        from orch.custody.base import CustodyError, KeyNotFound
        from orch.custody.convenience import secure_dir

        if not _WS.fullmatch(workspace_id):
            raise SessionError("bad workspace id")
        try:
            if create:
                self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            return secure_dir(self.root, (DIR, workspace_id), create=create)
        except KeyNotFound:
            if create:
                raise SessionError("the session directory vanished") from None
            return None
        except CustodyError as exc:
            raise SessionError(f"the session directory is unsafe: {exc}") from None
        except OSError as exc:
            raise SessionError(f"the session directory cannot be opened: {exc.strerror}") from None

    @staticmethod
    def _name(digest: str) -> str:
        if not _HASH.fullmatch(digest):
            raise SessionError("bad digest")
        return digest.split(":")[1] + ".json"

    def get(self, workspace_id: str, digest: str) -> dict[str, Any] | None:
        """The live session of ``digest``: ``None`` when there is none or it is older than :data:`TTL` (an expired one
        is not resumed). :class:`SessionError` when a record exists but does not validate (fail closed)."""
        from orch.custody.base import CustodyError, KeyNotFound
        from orch.custody.convenience import read_private

        fd = self._dir(workspace_id, create=False)
        if fd is None:
            return None
        try:
            try:
                data = read_private(fd, self._name(digest), limit=4 * 1024 * 1024)
            except KeyNotFound:
                return None
            except CustodyError as exc:
                raise SessionError(f"the session record is unreadable: {exc}") from None
        finally:
            os.close(fd)
        try:
            rec = canon.loads_strict(data)
        except Exception:  # noqa: BLE001 - any parse failure fails closed
            raise SessionError("the session record is not valid JSON") from None
        _validate(rec, workspace_id, digest)
        return None if self.wall() >= rec["expires"] else rec

    def put(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Write the record durably (temporary file, fsync, rename, directory sync)."""
        from orch.custody.base import CustodyError
        from orch.custody.convenience import write_private

        _validate(rec, rec["workspace_id"], rec["digest"])
        data = canon.dumps(rec)
        fd = self._dir(rec["workspace_id"], create=True)
        assert fd is not None
        try:
            write_private(fd, self._name(rec["digest"]), data, replace=True)
        except (CustodyError, OSError) as exc:
            raise SessionError(f"the session record cannot be written: {type(exc).__name__}") from None
        finally:
            os.close(fd)
        return rec

    def begin(self, workspace_id: str, person: str, digest: str, total: int) -> dict[str, Any]:
        """A fresh session. An old one under the same digest (expired, aborted or complete) is replaced: the person
        asked again."""
        now = int(self.wall())
        return self.put(
            {
                "v": 1,
                "workspace_id": workspace_id,
                "person": person,
                "digest": digest,
                "total": total,
                "created": now,
                "expires": now + int(TTL),
                "ticket": None,
                "steps": [],
                "aborted": None,
            }
        )

    def prune(self, workspace_id: str) -> None:
        """Remove this workspace's records that expired long ago (best effort; never raises)."""
        with contextlib.suppress(Exception):
            fd = self._dir(workspace_id, create=False)
            if fd is None:
                return
            try:
                from orch.custody.convenience import read_private

                for name in os.listdir(fd):
                    if not name.endswith(".json") or name.startswith("."):
                        continue
                    try:
                        rec = canon.loads_strict(read_private(fd, name, limit=4 * 1024 * 1024))
                        if self.wall() >= float(rec["expires"]) + TTL:
                            os.unlink(name, dir_fd=fd)
                    except Exception:  # noqa: BLE001
                        continue
            finally:
                os.close(fd)


def _validate(rec: Any, workspace_id: str, digest: str) -> None:
    def bad(why: str) -> SessionError:
        return SessionError(f"the session record is invalid: {why}")

    keys = {"v", "workspace_id", "person", "digest", "total", "created", "expires", "ticket", "steps", "aborted"}
    if not isinstance(rec, dict) or set(rec) != keys or rec.get("v") != 1:
        raise bad("shape")
    if rec["workspace_id"] != workspace_id or rec["digest"] != digest:
        raise bad("it names another session")
    if type(rec["total"]) is not int or not 1 <= rec["total"] <= MAX_STEPS:
        raise bad("total")
    for k in ("created", "expires"):
        if type(rec[k]) is not int:
            raise bad(k)
    t = rec["ticket"]
    if t is not None and (
        not isinstance(t, dict)
        or set(t) != {"key", "uid"}
        or not _KEY.fullmatch(str(t["key"]))
        or not _ULID.fullmatch(str(t["uid"]))
    ):
        raise bad("ticket")
    if not isinstance(rec["steps"], list) or len(rec["steps"]) > rec["total"]:
        raise bad("steps")
    for i, s in enumerate(rec["steps"]):
        if not isinstance(s, dict) or set(s) != {"k", "state", "request_id", "idem", "signed", "head"} or s["k"] != i:
            raise bad("a step")
        if s["state"] not in _STATES or not isinstance(s["request_id"], str) or not isinstance(s["idem"], str):
            raise bad("a step state")
        if (s["state"] == "claimed") != (s["signed"] is None) or (
            s["signed"] is not None and (not isinstance(s["signed"], list) or not s["signed"])
        ):
            raise bad("a step's events")
        if s["head"] is not None and (not isinstance(s["head"], str) or not _HASH.fullmatch(s["head"])):
            raise bad("a step's head")
    if rec["aborted"] is not None and not isinstance(rec["aborted"], str):
        raise bad("aborted")


# --- the engine ---------------------------------------------------------------------------------------------------


@dataclass
class Port:
    """What a custody path lends the engine. Every callable raises its own errors; the engine turns an unexpected one
    into an abort (what landed stays a draft)."""

    #: ``(op, args, sequence) -> prepared``: ``human.prepare`` bound to this signer's device
    prepare: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]
    #: the signer's own checks of a prepared request (:func:`orch.host.signer.check`), before anything is signed
    check: Callable[[dict[str, Any]], None]
    #: ``prepared -> signed events`` (the custody backend; one unlock for the whole sequence)
    sign: Callable[[dict[str, Any]], list[dict[str, Any]]]
    #: ``(request_id, events, idem) -> result`` (``human.submit``)
    submit: Callable[[str, list[dict[str, Any]], str], dict[str, Any]]
    new_idem: Callable[[], str]
    #: ``ticket key -> {"seq", "head"}``: the log head the host reports now (``ticket.get``'s ``doc.head``)
    head: Callable[[str], dict[str, Any]]
    cancel: Callable[[str], None] = field(default=lambda rid: None)
    #: reserves the signatures this run may still make (a rate budget); raises to refuse
    budget: Callable[[int], None] = field(default=lambda n: None)


class Driver:
    """Runs a sequence to its end or its abort. ``first`` of :meth:`run` is the already prepared, bound and checked
    first request (the one the person confirmed)."""

    def __init__(
        self,
        port: Port,
        sessions: Sessions,
        *,
        workspace_id: str,
        person: str,
        intent: Mapping[str, Any],
        on_step: Callable[[int, int], None] | None = None,
    ) -> None:
        self.port, self.sessions = port, sessions
        self.workspace_id, self.person = workspace_id, person
        self.intent = validate_intent(intent)
        self.digest = digest_of(workspace_id, person, self.intent)
        self.total = len(steps(self.intent))
        self.on_step = on_step

    def resumable(self) -> dict[str, Any] | None:
        """The live, not aborted, unfinished session of this intent, or ``None``."""
        rec = self.sessions.get(self.workspace_id, self.digest)
        if rec is None or rec["aborted"] is not None:
            return None
        if len(rec["steps"]) == self.total and all(s["state"] == "landed" for s in rec["steps"]):
            return None  # complete: the person asked again, a new ticket
        return rec

    @staticmethod
    def _landed(rec: dict[str, Any]) -> int:
        return sum(1 for s in rec["steps"] if s["state"] == "landed")

    def _abort(self, rec: dict[str, Any], reason: str) -> Aborted:
        rec["aborted"] = reason[:200]
        with contextlib.suppress(SessionError):
            self.sessions.put(rec)
        return Aborted(reason, landed=self._landed(rec), total=self.total)

    def _save(self, rec: dict[str, Any]) -> None:
        try:
            self.sessions.put(rec)
        except SessionError as exc:
            raise Aborted(f"{exc}; nothing more is signed", landed=self._landed(rec), total=self.total) from None

    def run(self, first: Mapping[str, Any] | None) -> dict[str, Any]:
        """Sign every step. With a live session this is a resume: ``first`` (a request prepared for the same intent
        before the person asked again) is cancelled, never signed. Returns ``{"key", "uid", "results", "landed"}``."""
        rec = self.resumable()
        if rec is None:
            if first is None:
                raise Aborted("there is nothing to resume, review again")
            rec = self.sessions.begin(self.workspace_id, self.person, self.digest, self.total)
        elif first is not None:
            self._cancel(first)
            first = None
        results: list[dict[str, Any]] = []
        prev_id: str | None = None
        prev_head: str | None = None
        who: dict[str, str] | None = rec["ticket"]
        self.port.budget(max(self.total - self._landed(rec), 0))
        for k in range(self.total):
            s = rec["steps"][k] if k < len(rec["steps"]) else None
            if s is not None and s["state"] == "landed":
                prev_id, prev_head = s["signed"][-1]["id"], s["head"]
                continue
            if s is not None and s["state"] == "claimed":
                # a crash between the claim and the signature: unknown whether a signature exists, never signed again
                raise self._abort(rec, "a signature was started and not recorded; it is not signed again")
            if s is not None:  # signed, not known to have landed: the exact bytes, the same idem, nothing else
                events, rid, idem = s["signed"], s["request_id"], s["idem"]
                try:
                    res = self.port.submit(rid, events, idem)
                except Exception as exc:  # noqa: BLE001 - the host's refusal or an outage: the draft stays
                    reason = f"step {k + 1} of {self.total} could not be sent again ({_why(exc)})"
                    raise self._abort(rec, reason) from None
            else:
                prepared = self._next(rec, k, first, who, prev_id, prev_head)
                if k == 0:
                    who = rec["ticket"]
                idem = self.port.new_idem()
                rid = prepared["request"]["id"]
                rec["steps"].append(
                    {"k": k, "state": "claimed", "request_id": rid, "idem": idem, "signed": None, "head": None}
                )
                self._save(rec)  # durable BEFORE the signature
                try:
                    events = self.port.sign(prepared)
                except BaseException as exc:
                    # an exception does not prove that no signature exists: the claim stays, never signed again
                    raise self._abort(rec, f"signing step {k + 1} of {self.total} failed ({_why(exc)})") from exc
                rec["steps"][k].update(state="signed", signed=events)
                self._save(rec)  # durable BEFORE the signature leaves the signer
                try:
                    res = self.port.submit(rid, events, idem)
                except Exception as exc:  # noqa: BLE001 - maybe not received: the next run resends these exact bytes
                    raise Aborted(
                        f"step {k + 1} of {self.total} was signed but the host did not confirm it ({_why(exc)}); "
                        "ask again within 10 minutes to send it again",
                        landed=self._landed(rec),
                        total=self.total,
                    ) from None
            results.append(res)
            prev_id, prev_head = events[-1]["id"], self._land(rec, k, res)
            if self.on_step:
                self.on_step(k + 1, self.total)
        assert who is not None
        return {"key": who["key"], "uid": who["uid"], "results": results, "landed": self.total}

    def _land(self, rec: dict[str, Any], k: int, res: Mapping[str, Any]) -> str | None:
        """The step landed: read the head the host reports and require it to be the head of the event this signer just
        signed (its ``seq`` is the submit's), so the next step chains to it. A foreign event already on top aborts."""
        head: str | None = None
        rec["steps"][k].update(state="landed", head=None)  # it landed: counted, whatever the head read says
        if k < self.total - 1:
            try:
                got = self.port.head(rec["ticket"]["key"])
            except Exception as exc:  # noqa: BLE001
                raise self._abort(rec, f"the ticket could not be read after step {k + 1} ({_why(exc)})") from None
            seq = res.get("seq")
            if type(seq) is not int or got.get("seq") != seq or not isinstance(got.get("head"), str):
                raise self._abort(rec, CHANGED)
            head = got["head"]
        rec["steps"][k]["head"] = head
        self._save(rec)
        return head

    def _next(
        self,
        rec: dict[str, Any],
        k: int,
        first: Mapping[str, Any] | None,
        who: dict[str, str] | None,
        prev_id: str | None,
        prev_head: str | None,
    ) -> dict[str, Any]:
        """The checked request of the new step ``k`` (the confirmed first one, or prepared after the last landed)."""
        prepared: dict[str, Any] | None = None
        try:
            if k == 0:
                if first is None:
                    raise Aborted("the first step is missing, review again")
                prepared = dict(first)
            else:
                assert who is not None
                op, args = step_args(self.intent, k, who["key"])
                meta = {"step": k, "of": self.total, "digest": self.digest, "prev": prev_id}
                prepared = self.port.prepare(op, args, meta)
                self.port.check(prepared)
            found = check_step(
                self.intent, k, prepared, person=self.person, first=who if k else None, prev_head=prev_head
            )
        except Aborted as exc:
            self._cancel(prepared)
            raise self._abort(rec, exc.reason) from None
        except Exception as exc:  # noqa: BLE001 - the host refused or vanished before anything was signed
            if k == 0:
                raise
            self._cancel(prepared)
            raise self._abort(rec, f"step {k + 1} of {self.total} was refused ({_why(exc)})") from None
        if k == 0:
            rec["ticket"] = found
        return prepared

    def _cancel(self, prepared: Mapping[str, Any] | None) -> None:
        with contextlib.suppress(Exception):
            if prepared is not None:
                self.port.cancel(prepared["request"]["id"])


def _why(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) else type(exc).__name__
