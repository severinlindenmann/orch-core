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
    "ADDON",
    "EDIT",
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
    "label_of",
    "step_args",
    "steps",
    "validate_intent",
]

#: the key of a prepared request's events. Spelled in two parts on purpose: it is a request field, not the store's
#: ``events`` directory that the layout lint (tests/test_layout_lint.py) looks for.
_EVENTS = "event" + "s"

KIND = "new_ticket"
QUICK = "quick_task"
#: B12D: the same engine over an EXISTING ticket: edited sections, acceptance criteria and people, ONE review
EDIT = "edit_ticket"
#: turning on an add-on that needs settings: ``settings.set`` then ``addon.grant``, on the workspace log (seqaddon.py)
ADDON = "addon_enable"
VERSION = 1
MAX_STEPS = 16
MAX_SECTION_BYTES = 65536
TTL = 600.0  # a session lives as long as a request does (§0.8.1): 10 minutes
CHANGED = "the ticket changed while signing, review again"
#: the same for the add-on sequence, which has no ticket
CHANGED_WS = "the workspace changed while signing, review again"

_TYPES = ("feature", "bug", "chore", "spike", "epic")
_PRIORITIES = ("low", "medium", "high", "urgent")
_SIZES = ("xs", "s", "m", "l", "xl")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})$")
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PERSON = re.compile(r"^p_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^T[0-9]+$")
MAX_TASKS = 32
_AC_ID = re.compile(r"^AC[1-9][0-9]*$")
MAX_AC = 8  # acceptance criteria one sequence adds or edits
#: R3 part B: pasted screenshots one sequence adds (one signed ``artifact.add`` each, before the section showing it)
MAX_IMAGES = 6
IMAGE_MAX_BYTES = 5 * 1024 * 1024
#: the name the dashboard gives a pasted picture: when it was pasted and the first 8 hex of its digest (so a name can
#: never stand for other bytes than the ones it was made from); a F1 section 6 name
_IMAGE_NAME = re.compile(r"^paste-[0-9]{8}-[0-9]{6}-([0-9a-f]{8})\.(?:png|webp)$")
_IMAGE_KEYS = frozenset({"name", "sha256", "bytes", "alt"})
_ARTIFACT_REF = re.compile(r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)")  # store.render._REFS, the same text rule
MAX_PEOPLE = 16
#: the people roles a sequence sets, in the order they are signed: the owner LAST (after it the signer may no longer
#: manage the ticket if the signer is only a member, so nothing else may follow it)
PEOPLE_ROLES = ("assignees", "reviewers", "owner")
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

    def __init__(self, reason: str, *, landed: int = 0, total: int = 0, resumable: bool = False) -> None:
        super().__init__(reason)
        #: ``resumable``: the session is still live (a step was signed and sent but not confirmed): the same review
        #: asked again finishes it; otherwise what landed stays a draft and asking again makes a new ticket
        self.reason, self.landed, self.total, self.resumable = reason, landed, total, resumable


class SessionError(Exception):
    """The session record cannot be written, read or validated: the caller fails closed."""


# --- the intent ---------------------------------------------------------------------------------------------------


def validate_intent(intent: Any) -> dict[str, Any]:
    """The intent in its one canonical form, or :class:`SequenceInvalid`. Texts must already be normalised (CRLF, NFC,
    no surrounding line feeds): the signer never changes what it was given, it refuses."""
    if isinstance(intent, Mapping) and intent.get("kind") == QUICK:
        from . import seqquick

        return seqquick.validate(intent, SequenceInvalid)
    if isinstance(intent, Mapping) and intent.get("kind") == ADDON:
        from . import seqaddon

        return seqaddon.validate(intent, SequenceInvalid)
    if isinstance(intent, Mapping) and intent.get("kind") == EDIT:
        return _validate_edit(intent)
    return _validate_new_ticket(intent)


def _one_line(value: Any, what: str, limit: int) -> str:
    from orch.canon.text import TextError, normalize_text

    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise SequenceInvalid(f"{what} is 1 to {limit} characters")
    try:
        if normalize_text(value, one_line=True) != value:
            raise SequenceInvalid(f"{what} is not normalised")
    except TextError as exc:
        raise SequenceInvalid(f"{what}: {exc}") from None
    return value


def _people_ids(value: Any, what: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_PEOPLE
        or any(not isinstance(x, str) or not _PERSON.fullmatch(x) for x in value)
        or value != sorted(set(value))
    ):
        raise SequenceInvalid(f"{what}: up to {MAX_PEOPLE} person ids, sorted, no repeats")
    return list(value)


def _new_people(value: Any) -> dict[str, Any]:
    """The people of a NEW ticket: ``owner`` (one id) and ``assignees``/``reviewers`` (sorted ids)."""
    if not isinstance(value, Mapping) or not value or not set(value) <= set(PEOPLE_ROLES):
        raise SequenceInvalid("people holds owner, assignees and reviewers, and something in it")
    out: dict[str, Any] = {}
    for role in PEOPLE_ROLES:
        if role not in value:
            continue
        if role == "owner":
            if not isinstance(value[role], str) or not _PERSON.fullmatch(value[role]):
                raise SequenceInvalid("the owner is one person id")
            out[role] = value[role]
        else:
            ids = _people_ids(value[role], role)
            if not ids:
                raise SequenceInvalid(f"{role}: leave it out instead of sending none")
            out[role] = ids
    return out


def _images(value: Any, texts: list[str]) -> list[dict[str, Any]]:
    """The pasted screenshots of an intent: at most :data:`MAX_IMAGES`, sorted by name, each ``{name, sha256, bytes,
    alt}``, each shown by at least one of the written ``texts`` (``![alt](artifact:NAME)``): nothing is signed that no
    text shows. The bytes are not in the intent: the signer holds them and checks them against ``sha256``."""
    from orch.canon.text import TextError, normalize_text

    if not isinstance(value, list) or not 1 <= len(value) <= MAX_IMAGES:
        raise SequenceInvalid(f"images: 1 to {MAX_IMAGES} screenshots (leave it out for none)")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _IMAGE_KEYS:
            raise SequenceInvalid("an image is {name, sha256, bytes, alt}")
        name, digest, size, alt = item["name"], item["sha256"], item["bytes"], item["alt"]
        m = _IMAGE_NAME.fullmatch(name) if isinstance(name, str) else None
        if m is None:
            raise SequenceInvalid("an image name is paste-<date>-<time>-<digest>.png (or .webp)")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or digest[len("sha256:") :][:8] != m.group(1):
            raise SequenceInvalid("an image name carries the first 8 characters of its digest")
        if type(size) is not int or not 1 <= size <= IMAGE_MAX_BYTES:
            raise SequenceInvalid("a screenshot is 1 byte to 5 MiB")
        if not isinstance(alt, str) or not 1 <= len(alt) <= 120:
            raise SequenceInvalid("an image description is 1 to 120 characters")
        try:
            if normalize_text(alt, one_line=True) != alt:
                raise SequenceInvalid("an image description is not normalised")
        except TextError as exc:
            raise SequenceInvalid(f"an image description: {exc}") from None
        out.append({"name": name, "sha256": digest, "bytes": size, "alt": alt})
    names = [i["name"] for i in out]
    if names != sorted(set(names)):
        raise SequenceInvalid("images are sorted by name, without repeats")
    shown = {n for t in texts for n in _ARTIFACT_REF.findall(t)}
    if any(n not in shown for n in names):
        raise SequenceInvalid("every screenshot is shown by a written section (![...](artifact:NAME))")
    return out


def _validate_edit(intent: Mapping[str, Any]) -> dict[str, Any]:
    """An edit of an existing ticket: the ticket (key and log id), the head the person reviewed, and what changes."""
    from orch.canon.text import TextError, normalize_text
    from orch.schema import SECTIONS_BY_TYPE

    base = {"v", "kind", "ticket", "ticket_type", "head", "sections"}
    if not base <= set(intent) <= base | {"acceptance", "tasks", "people", "links", "images"}:
        raise SequenceInvalid(
            "an edit has exactly v, kind, ticket, ticket_type, head, sections, acceptance, tasks, links, images "
            "and people"
        )
    if intent["v"] != VERSION:
        raise SequenceInvalid("this signer does not know that sequence")
    ticket, ttype, head = intent["ticket"], intent["ticket_type"], intent["head"]
    if (
        not isinstance(ticket, Mapping)
        or set(ticket) != {"key", "uid"}
        or not isinstance(ticket["key"], str)
        or not _KEY.fullmatch(ticket["key"])
        or not isinstance(ticket["uid"], str)
        or not _ULID.fullmatch(ticket["uid"])
    ):
        raise SequenceInvalid("ticket is {key, uid}")
    if ttype not in _TYPES:
        raise SequenceInvalid("the ticket type is unknown")
    if not isinstance(head, str) or not _HASH.fullmatch(head):
        raise SequenceInvalid("head is the hash of the log head that was reviewed")
    sections = intent["sections"]
    if not isinstance(sections, Mapping):
        raise SequenceInvalid("sections is a map")
    out_sections: dict[str, Any] = {}
    for sid in sorted(sections):
        entry = sections[sid]
        if sid not in SECTIONS_BY_TYPE[ttype] or not isinstance(entry, Mapping) or set(entry) != {"was", "text"}:
            raise SequenceInvalid(f"a {ttype} ticket has no section {str(sid)[:40]!r} or it is not {{was, text}}")
        text, was = entry["text"], entry["was"]
        if not isinstance(was, str) or not _HASH.fullmatch(was):
            raise SequenceInvalid(f"section {sid}: was is a section hash")
        if not isinstance(text, str) or not text or text != text.strip("\n") or len(text.encode()) > MAX_SECTION_BYTES:
            raise SequenceInvalid(f"section {sid}: empty, with surrounding line feeds, or longer than 64 KiB")
        try:
            if normalize_text(text) != text:
                raise SequenceInvalid(f"section {sid} is not normalised")
        except TextError as exc:
            raise SequenceInvalid(f"section {sid}: {exc}") from None
        if canon.section_hash(text) == was:
            raise SequenceInvalid(f"section {sid}: the text is the one it replaces")
        out_sections[sid] = {"was": was, "text": text}
    out: dict[str, Any] = {
        "v": VERSION,
        "kind": EDIT,
        "ticket": {"key": ticket["key"], "uid": ticket["uid"]},
        "ticket_type": ttype,
        "head": head,
        "sections": out_sections,
    }
    if "acceptance" in intent:
        out["acceptance"] = _edit_acceptance(intent["acceptance"])
    if "tasks" in intent:
        out["tasks"] = _edit_tasks(intent["tasks"], out.get("acceptance", {}))
    if "links" in intent:
        out["links"] = _edit_links(intent["links"])
    if "people" in intent:
        out["people"] = _edit_people(intent["people"])
    if "images" in intent:
        out["images"] = _images(intent["images"], [e["text"] for e in out_sections.values()])
    if len(steps(out)) > MAX_STEPS:
        raise SequenceInvalid("too many changes for one review")
    if not steps(out):
        raise SequenceInvalid("nothing changes")
    return out


def _edit_acceptance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"was", "edit", "add"}:
        raise SequenceInvalid("acceptance is {was, edit, add}")
    was, edit, add = value["was"], value["edit"], value["add"]
    if not isinstance(was, list) or len(was) > 64:
        raise SequenceInvalid("acceptance.was is the list that was reviewed")
    out_was: list[dict[str, str]] = []
    for item in was:
        if not isinstance(item, Mapping) or set(item) != {"id", "text"} or not isinstance(item["id"], str):
            raise SequenceInvalid("acceptance.was: {id, text}")
        if not _AC_ID.fullmatch(item["id"]) or not isinstance(item["text"], str):
            raise SequenceInvalid("acceptance.was: a criterion id and its text")
        out_was.append({"id": item["id"], "text": item["text"]})
    if len({a["id"] for a in out_was}) != len(out_was):
        raise SequenceInvalid("acceptance.was: a criterion twice")
    if not isinstance(edit, Mapping) or not isinstance(add, list) or len(edit) + len(add) > MAX_AC:
        raise SequenceInvalid(f"acceptance: at most {MAX_AC} changes")
    known = {a["id"]: a["text"] for a in out_was}
    out_edit: dict[str, str] = {}
    for aid in sorted(edit, key=lambda x: int(x[2:]) if _AC_ID.fullmatch(str(x)) else 0):
        if aid not in known:
            raise SequenceInvalid(f"acceptance.edit: there is no criterion {str(aid)[:20]!r}")
        text = _one_line(edit[aid], "a criterion", 200)
        if text == known[aid]:
            raise SequenceInvalid(f"acceptance.edit: {aid} is the same text")
        out_edit[aid] = text
    out_add = [_one_line(t, "a criterion", 200) for t in add]
    return {"was": out_was, "edit": out_edit, "add": out_add}


def _task_proves(value: Any, known: set[str] | None = None) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(a, str) or not _AC_ID.fullmatch(a) for a in value)
        or value != sorted(set(value), key=lambda a: int(a[2:]))
        or (known is not None and not set(value) <= known)
    ):
        raise SequenceInvalid("task proves: sorted unique known criterion ids")
    return list(value)


def _task_verify(value: Any) -> str:
    cmd = _one_line(value, "verify command", 4096)
    if len(cmd.encode("utf-8")) > 4096:  # Call.text's byte limit
        raise SequenceInvalid("verify command is longer than 4096 bytes")
    from orch.model.tasks import verify_cmd_problem

    if problem := verify_cmd_problem(cmd):
        raise SequenceInvalid(f"verify command: {problem}")
    return cmd


def _edit_tasks(value: Any, acceptance: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"was", "edit", "add"}:
        raise SequenceInvalid("tasks is {was, edit, add}")
    was, edit, add = value["was"], value["edit"], value["add"]
    if not isinstance(was, list) or len(was) > 64:
        raise SequenceInvalid("tasks.was is up to 64 reviewed tasks")
    known: dict[str, Any] = {}
    for item in was:
        required = {"id", "text", "verify", "proves"}
        if not isinstance(item, Mapping) or not required <= set(item) <= required | {"assignee"}:
            raise SequenceInvalid("tasks.was: {id, text, verify, proves, assignee?}")
        tid = item["id"]
        if not isinstance(tid, str) or not _TASK_ID.fullmatch(tid) or tid in known:
            raise SequenceInvalid("tasks.was: unique task ids")
        if not isinstance(item["text"], str):
            raise SequenceInvalid("tasks.was: task text")
        verify = item["verify"]
        if verify is not None and (
            not isinstance(verify, Mapping) or set(verify) != {"cmd"} or not isinstance(verify["cmd"], str)
        ):
            raise SequenceInvalid("tasks.was: verify is {cmd} or null")
        _task_proves(item["proves"])
        if "assignee" in item and (not isinstance(item["assignee"], str) or not _PERSON.fullmatch(item["assignee"])):
            raise SequenceInvalid("tasks.was: assignee is a person id")
        known[tid] = copy.deepcopy(dict(item))
    if not isinstance(edit, Mapping) or not isinstance(add, list) or len(edit) + len(add) > MAX_TASKS:
        raise SequenceInvalid(f"tasks: at most {MAX_TASKS} changes")
    ac_ids = {a["id"] for a in acceptance.get("was", [])}
    last = max((int(a[2:]) for a in ac_ids), default=0)
    ac_ids.update(f"AC{last + i}" for i in range(1, len(acceptance.get("add", [])) + 1))

    def fields(item: Any, *, adding: bool) -> dict[str, Any]:
        if (
            not isinstance(item, Mapping)
            or not item
            or not set(item) <= {"text", "verify", "proves"}
            or (adding and not {"text", "proves"} <= set(item))
        ):
            raise SequenceInvalid("a task change has text, verify and/or proves")
        out: dict[str, Any] = {}
        if "text" in item:
            out["text"] = _one_line(item["text"], "task text", 500)
        if "verify" in item:
            out["verify"] = None if item["verify"] is None and not adding else _task_verify(item["verify"])
        if "proves" in item:
            out["proves"] = _task_proves(item["proves"], ac_ids)
        return out

    out_edit = {}
    for tid in edit:
        if not isinstance(tid, str) or tid not in known:
            raise SequenceInvalid("tasks.edit: unknown task")
    for tid in sorted(edit, key=lambda t: (int(t[1:]), t)):
        change = fields(edit[tid], adding=False)
        old = known[tid]
        previous = {**old, "verify": old["verify"]["cmd"] if old["verify"] else None}
        if any(v == previous[key] for key, v in change.items()):
            raise SequenceInvalid("tasks.edit: each supplied field must change")
        out_edit[tid] = change
    return {"was": list(known.values()), "edit": out_edit, "add": [fields(a, adding=True) for a in add]}


_LINK_KEYS = ("repos", "branches", "prs", "external")
_REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_REPOS = 16


def _link_repos(value: Any, what: str, *, ordered: bool = True) -> list[str]:
    """Repository names, no repeats; the new list is sorted (the reviewed ``was`` keeps whatever order it has)."""
    if (
        not isinstance(value, list)
        or len(value) > MAX_REPOS
        or any(not isinstance(x, str) or not _REPO_NAME.fullmatch(x) for x in value)
        or len(set(value)) != len(value)
        or (ordered and value != sorted(value))
    ):
        raise SequenceInvalid(
            f"{what}: up to {MAX_REPOS} repository names, no repeats" + (", sorted" if ordered else "")
        )
    return list(value)


def _link_branches(value: Any, repos: list[str], what: str) -> dict[str, str]:
    """``{repo: branch}`` for linked repos only; every branch is a valid git branch name (the rule the log applies)."""
    from orch.schema import SchemaError, _check_ref

    if not isinstance(value, Mapping) or not set(value) <= set(repos):
        raise SequenceInvalid(f"{what}: a branch only for a linked repository")
    out: dict[str, str] = {}
    for repo in sorted(value):
        branch = value[repo]
        if not isinstance(branch, str) or not 1 <= len(branch) <= 200 or branch != branch.strip():
            raise SequenceInvalid(f"{what}: the branch of {repo} is 1 to 200 characters")
        try:
            _check_ref("sequence", ("links", "branches", repo), "refs/heads/" + branch)
        except SchemaError:
            raise SequenceInvalid(f"{what}: {branch[:60]!r} is not a valid branch name") from None
        out[repo] = branch
    return out


def _edit_links(value: Any) -> dict[str, Any]:
    """``{was, repos, branches}``: the whole links value the person saw (its hash is the edit's ``base_rev``) and the
    repositories and branches it becomes. The pull requests and external links the person did not touch stay as they
    were; a repository that has a pull request cannot be unlinked here."""
    if not isinstance(value, Mapping) or set(value) != {"was", "repos", "branches"}:
        raise SequenceInvalid("links is {was, repos, branches}")
    was = value["was"]
    if not isinstance(was, Mapping) or set(was) != set(_LINK_KEYS):
        raise SequenceInvalid("links.was is the links value that was reviewed")
    was_repos = _link_repos(was["repos"], "links.was", ordered=False)
    if not isinstance(was["branches"], Mapping) or not set(was["branches"]) <= set(was_repos):
        raise SequenceInvalid("links.was: a branch only for a linked repository")
    was_branches = dict(was["branches"])  # as the log holds it; the schema checked it when it was written
    if not isinstance(was["prs"], list) or not isinstance(was["external"], list):
        raise SequenceInvalid("links.was: prs and external are lists")
    repos = _link_repos(value["repos"], "links")
    branches = _link_branches(value["branches"], repos, "links")
    if any(not isinstance(p, Mapping) or p.get("repo") not in repos for p in was["prs"]):
        raise SequenceInvalid("a repository with a pull request cannot be unlinked here")
    if set(repos) == set(was_repos) and branches == was_branches:
        raise SequenceInvalid("links: nothing changes")
    return {
        "was": {
            "repos": was_repos,
            "branches": was_branches,
            "prs": copy.deepcopy(was["prs"]),
            "external": copy.deepcopy(was["external"]),
        },
        "repos": repos,
        "branches": branches,
    }


def _links_after(links: Mapping[str, Any]) -> dict[str, Any]:
    """The links value after the edit: the reviewed one with the new repositories and branches."""
    return {**copy.deepcopy(dict(links["was"])), "repos": list(links["repos"]), "branches": dict(links["branches"])}


def _edit_people(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value or not set(value) <= set(PEOPLE_ROLES):
        raise SequenceInvalid("people holds owner, assignees and reviewers, and something in it")
    out: dict[str, Any] = {}
    for role in PEOPLE_ROLES:
        if role not in value:
            continue
        ch = value[role]
        if not isinstance(ch, Mapping) or set(ch) != {"add", "remove"}:
            raise SequenceInvalid(f"people.{role} is {{add, remove}}")
        add, remove = _people_ids(ch["add"], f"{role} add"), _people_ids(ch["remove"], f"{role} remove")
        if set(add) & set(remove) or not (add or remove):
            raise SequenceInvalid(f"people.{role}: nothing to change, or a person added and removed")
        if role == "owner" and (len(add) != 1 or remove):
            raise SequenceInvalid("the owner is replaced by exactly one person")
        out[role] = {"add": add, "remove": remove}
    return out


def _validate_new_ticket(intent: Any) -> dict[str, Any]:
    from orch.canon.text import TextError, normalize_text
    from orch.schema import SECTIONS_BY_TYPE

    def bad(why: str) -> SequenceInvalid:
        return SequenceInvalid(why)

    base = {"v", "kind", "title", "ticket_type", "set", "sections"}
    # B12D: acceptance and people are optional (left out when empty, so every earlier intent keeps its digest)
    if not isinstance(intent, Mapping) or not base <= set(intent) <= base | {"acceptance", "people", "agent", "images"}:
        raise bad("an intent has exactly v, kind, title, ticket_type, set, sections, acceptance, people, images, agent")
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
    result: dict[str, Any] = {
        "v": VERSION,
        "kind": KIND,
        "title": title,
        "ticket_type": kind,
        "set": out_set,
        "sections": out_sections,
    }
    if "acceptance" in intent:
        ac = intent["acceptance"]
        if not isinstance(ac, list) or not 1 <= len(ac) <= MAX_AC:
            raise bad(f"acceptance: 1 to {MAX_AC} criteria (leave it out for none)")
        result["acceptance"] = [_one_line(t, "a criterion", 200) for t in ac]
    if "people" in intent:
        result["people"] = _new_people(intent["people"])
    if "images" in intent:
        result["images"] = _images(intent["images"], list(out_sections.values()))
    if "agent" in intent:  # U4: the runner grant, signed last in the same confirmation (left out when none)
        from . import seqquick

        result["agent"] = seqquick.validate_agent(intent["agent"], SequenceInvalid)
    if len(steps(result)) > MAX_STEPS:
        raise bad("too many changes for one review")
    return result


def label_of(intent: Mapping[str, Any]) -> str:
    """What names the sequence in a prompt: the title of a new ticket or quick task, ``edit KEY`` for an edit (which has
    no title: it changes a ticket that already exists)."""
    if intent["kind"] == EDIT:
        return f"edit {intent['ticket']['key']}"
    if intent["kind"] == ADDON:
        return f"turn on {intent['name']}"
    return str(intent["title"])


def digest_of(workspace_id: str, person: str, intent: Mapping[str, Any]) -> str:
    """``sha256:`` over the whole intent, bound to the workspace and the person (the key of a session)."""
    blob = canon.dumps({"workspace_id": workspace_id, "person": person, "intent": intent})
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ordinary person ops of the sequence, in order, as ``{"op", "args", "kind", "ref"}`` (``ref``: the op names
    the ticket, added once the key is known, :func:`step_args`)."""
    if intent["kind"] == QUICK:
        from . import seqquick

        return seqquick.steps(intent)
    if intent["kind"] == ADDON:
        from . import seqaddon

        return seqaddon.steps(intent)
    out: list[dict[str, Any]] = []
    if intent["kind"] == KIND:
        out.append(
            {
                "op": "new",
                "args": {"title": intent["title"], "type": intent["ticket_type"]},
                "kind": "new",
                "ref": False,
            }
        )
        sets = intent["set"]
        if sets:
            pairs = []
            for k in _SET_KEYS:
                if k in sets:
                    pairs.append(f"{k}={','.join(sets[k]) if k == 'labels' else sets[k]}")
            out.append({"op": "set", "args": {"pairs": pairs}, "kind": "set", "ref": True})
        out.extend(_image_steps(intent))  # before the sections that show them: a reference to a missing file is refused
        for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
            out.append(
                {
                    "op": "section.set",
                    "args": {"section": sid, "message": intent["sections"][sid]},
                    "kind": "section",
                    "ref": True,
                }
            )
        for text in intent.get("acceptance", ()):
            out.append({"op": "ac.add", "args": {"text": text}, "kind": "ac", "ref": True})
        for role in PEOPLE_ROLES:
            if role in intent.get("people", {}):
                who = intent["people"][role]
                add = [who] if role == "owner" else list(who)
                args = {"role": role, "add": add, "remove": []}
                out.append({"op": "people.set", "args": args, "kind": "people", "ref": True})
        if "agent" in intent:  # always LAST: a prefix without it is a ticket and no agent access
            from . import seqquick

            out.append(seqquick.grant_step(intent["agent"]))
        return out
    # EDIT: pasted screenshots, sections, acceptance, tasks, links, then people (owner last)
    out.extend(_image_steps(intent))
    for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
        out.append(
            {
                "op": "section.set",
                "args": {"section": sid, "message": intent["sections"][sid]["text"]},
                "kind": "section",
                "ref": True,
            }
        )
    ac = intent.get("acceptance")
    if ac:
        for aid, text in ac["edit"].items():
            out.append({"op": "ac.edit", "args": {"ac": aid, "text": text}, "kind": "ac", "ref": True})
        for text in ac["add"]:
            out.append({"op": "ac.add", "args": {"text": text}, "kind": "ac", "ref": True})
    tasks = intent.get("tasks")
    if tasks:
        for tid in sorted(tasks["edit"], key=lambda t: (int(t[1:]), t)):
            change = tasks["edit"][tid]
            args = {"task": tid, **{key: "" if v is None else v for key, v in change.items()}}
            out.append({"op": "task.edit", "args": args, "kind": "task", "ref": True})
        for task in tasks["add"]:
            out.append({"op": "task.add", "args": copy.deepcopy(task), "kind": "task", "ref": True})
    if "links" in intent:
        lk = intent["links"]
        pair = "links=" + canon.dumps({"repos": lk["repos"], "branches": lk["branches"]}).decode()
        out.append({"op": "set", "args": {"pairs": [pair]}, "kind": "links", "ref": True})
    for role in PEOPLE_ROLES:
        if role in intent.get("people", {}):
            ch = intent["people"][role]
            out.append(
                {
                    "op": "people.set",
                    "args": {"role": role, "add": list(ch["add"]), "remove": list(ch["remove"])},
                    "kind": "people",
                    "ref": True,
                }
            )
    return out


def _image_steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One ``artifact.add`` per pasted screenshot: its file is uploaded by the signer, never named by a path."""
    return [
        {
            "op": "artifact.add",
            "args": {"path": i["name"], "name": i["name"], "kind": "screenshot", "label": i["alt"]},
            "kind": "image",
            "ref": True,
        }
        for i in intent.get("images", ())
    ]


def step_args(intent: Mapping[str, Any], k: int, ref: str | None) -> tuple[str, dict[str, Any]]:
    """``(op, args)`` of step ``k`` for ``human.prepare``: the steps that act on the ticket name it by ``ref``."""
    step = steps(intent)[k]
    args = copy.deepcopy(step["args"])
    if step["ref"]:
        if ref is None and intent["kind"] == EDIT:
            ref = intent["ticket"]["key"]  # an edit names its ticket from the first step on
        args = {"ref": ref, **args}
    return step["op"], args


def confirm_kind(intent: Mapping[str, Any]) -> str:
    """The strongest confirmation any step needs (``click`` < ``terminal``); the sequence's confirmation is that of its
    strongest step. #1198 C1: there is no typed word any more, so every people change and a grant to the runner are a
    click (the grant also needs dashboard execution on this machine: the click signer checks that on its own). Only a
    step whose op is terminal-only (``settings.set`` of Publish) makes the whole sequence terminal."""
    from orch.click.protocol import op_class

    def step_class(s: Mapping[str, Any]) -> str:
        if s["kind"] == "grant":
            return "click"
        if s["op"] == "addon.grant" and intent["kind"] == ADDON:
            return op_class(s["op"], events=[intent["grant"]])  # the signed capability list decides
        # AE3: the class of a step is that of its op WITH its args (``settings.set`` of Publish is terminal-only)
        return op_class(s["op"], args=s["args"])

    return "terminal" if any(step_class(s) == "terminal" for s in steps(intent)) else "click"


def has_grant(intent: Mapping[str, Any]) -> bool:
    return any(s["kind"] == "grant" for s in steps(intent))


def expected_payload(intent: Mapping[str, Any], k: int, person: str) -> dict[str, Any]:
    """The payload (type and payload keys, no envelope) the event of step ``k`` must have. Step 0 has no ``key``: the
    host allocates it (:func:`check_step` takes it from the prepared event and format-checks it)."""
    from orch.store.render import section_entry

    if intent["kind"] == QUICK:
        from . import seqquick

        return seqquick.payloads(intent, person)[k]
    if intent["kind"] == ADDON:
        from . import seqaddon

        return seqaddon.payload(intent, k)
    plan = steps(intent)
    if plan[k]["kind"] == "grant":
        from . import seqquick

        return seqquick.grant_payload(intent["agent"])
    if k == 0 and intent["kind"] == KIND:
        return {
            "type": "ticket.created",
            "ticket_type": intent["ticket_type"],
            "title": intent["title"],
            "owner": person,
        }
    step = plan[k]
    op = step["op"]
    if step["kind"] == "image":
        img = next(i for i in intent["images"] if i["name"] == step["args"]["name"])
        return {
            "type": "artifact.added",
            "name": img["name"],
            "kind": "screenshot",
            "label": img["alt"],
            "sha256": img["sha256"],
            "bytes": img["bytes"],
        }
    if step["kind"] == "links":
        return {
            "type": "ticket.updated",
            "base_rev": {"ticket.links": canon.value_hash(intent["links"]["was"])},
            "set": {"ticket.links": _links_after(intent["links"])},
        }
    if op == "set":
        sets = {f"ticket.{key}": copy.deepcopy(v) for key, v in intent["set"].items()}
        return {
            "type": "ticket.updated",
            "base_rev": {path: canon.value_hash(_DEFAULTS[path[len("ticket.") :]]) for path in sets},
            "set": sets,
        }
    if op == "people.set":
        a = step["args"]
        return {"type": "people.changed", "role": a["role"], "add": list(a["add"]), "remove": list(a["remove"])}
    if op in ("ac.add", "ac.edit"):
        before, after = _acceptance_around(intent, plan, k)
        return {
            "type": "ticket.updated",
            "base_rev": {"ticket.acceptance": canon.value_hash(before)},
            "set": {"ticket.acceptance": after},
        }
    if step["kind"] == "task":
        before, after = _tasks_around(intent, plan, k)
        return {
            "type": "ticket.updated",
            "base_rev": {"ticket.tasks": canon.value_hash(before)},
            "set": {"ticket.tasks": after},
        }
    sid = step["args"]["section"]
    was = canon.section_hash("") if intent["kind"] == KIND else intent["sections"][sid]["was"]
    return {
        "type": "ticket.updated",
        "base_rev": {f"body.{sid}": was},
        "sections": {sid: section_entry(step["args"]["message"])},
    }


def _acceptance_around(
    intent: Mapping[str, Any], plan: list[dict[str, Any]], k: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The criteria list before and after step ``k`` (``ac.add`` appends ``AC<max+1>``; ``ac.edit`` changes one text),
    from the intent alone: the reviewed list plus the changes the earlier steps made."""
    was = intent["acceptance"]["was"] if intent["kind"] == EDIT and "acceptance" in intent else []
    items: list[dict[str, str]] = [dict(a) for a in was]
    for i in range(k + 1):
        if plan[i]["kind"] != "ac":
            continue
        before = [dict(a) for a in items]
        if plan[i]["op"] == "ac.add":
            n = max((int(a["id"][2:]) for a in items), default=0) + 1
            items.append({"id": f"AC{n}", "text": plan[i]["args"]["text"]})
        else:
            items = [{**a, "text": plan[i]["args"]["text"]} if a["id"] == plan[i]["args"]["ac"] else a for a in items]
        if i == k:
            return before, [dict(a) for a in items]
    raise SequenceInvalid("not an acceptance step")


def _tasks_around(
    intent: Mapping[str, Any], plan: list[dict[str, Any]], k: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay the reviewed tasks and earlier task steps, without trusting host state."""
    items = copy.deepcopy(intent["tasks"]["was"])
    for i in range(k + 1):
        step = plan[i]
        if step["kind"] != "task":
            continue
        before = copy.deepcopy(items)
        args = step["args"]
        if step["op"] == "task.add":
            n = max((int(t["id"][1:]) for t in items), default=0) + 1
            items.append(
                {
                    "id": f"T{n}",
                    "text": args["text"],
                    "verify": {"cmd": args["verify"]} if args.get("verify") else None,
                    "proves": list(args["proves"]),
                }
            )
        elif step["op"] == "task.edit":
            task = next(t for t in items if t["id"] == args["task"])
            for key in ("text", "verify", "proves"):
                if key in args:
                    task[key] = (
                        ({"cmd": args[key]} if args[key] else None) if key == "verify" else copy.deepcopy(args[key])
                    )
        else:
            raise SequenceInvalid("unknown task step")
        if i == k:
            return before, copy.deepcopy(items)
    raise SequenceInvalid("not a task step")


_ENVELOPE = frozenset({"v", "id", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"})


def check_step(
    intent: Mapping[str, Any],
    k: int,
    prepared: Mapping[str, Any],
    *,
    person: str,
    first: Mapping[str, str] | None,
    prev_head: str | None,
    done: Mapping[int, str] | None = None,
    now: float | None = None,
) -> dict[str, str] | None:
    """Refuse (:class:`Aborted`) unless the request the host prepared for step ``k`` is, byte for byte, the event the
    reviewed intent says, on the ticket this sequence created, chained to the event this signer signed before it.
    ``done`` maps a step number to the id of the event this signer signed there (a gate approval's ``prior`` must name
    the requirements approval it signed itself). Returns ``{"key", "uid"}`` of the ticket."""

    def stop() -> Aborted:
        return Aborted(CHANGED_WS if intent["kind"] == ADDON else CHANGED)

    if intent["kind"] == ADDON:  # a workspace sequence: no ticket (the result is None)
        from . import seqaddon

        seqaddon.check_step(intent, k, prepared, stop)
        return None
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
    ev, kind = events[0], plan[k]["kind"]
    opened = _open_keys(kind)
    got = {key: v for key, v in ev.items() if key not in _ENVELOPE and key not in opened}
    want = expected_payload(intent, k, person)
    log, material = prepared.get("log"), prepared.get("material") or {}
    if not isinstance(material, Mapping):
        raise stop()
    ticket = material.get("ticket")
    if k == 0 and intent["kind"] == EDIT:
        # an edit starts on the ticket the person reviewed, at the head they reviewed: anything else is another edit
        t = intent["ticket"]
        who = {"key": t["key"], "uid": t["uid"]}
        if log != t["uid"] or ticket != who or ev.get("based_on") != intent["head"]:
            raise stop()
        if first is not None and dict(first) != who:
            raise stop()
        _check_content(kind, want, material, plan, k, stop)
    elif k == 0:
        key = got.pop("key", None)  # the one host-chosen value: allocated by the host, shown "per host, not proven"
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(log, str) or not _ULID.fullmatch(log):
            raise stop()
        who: dict[str, str] = {"key": key, "uid": log}
        if first is not None and dict(first) != who:
            raise stop()  # a resume: the first step was signed for another ticket
    elif kind == "grant":
        if first is None or log != "workspace" or ticket is not None or material.get("kind") != "payload":
            raise stop()
        who = {"key": first["key"], "uid": first["uid"]}
        if "sequence" in material or material.get("before") or material.get("texts"):
            raise stop()
        _check_grant(ev, intent, now)
    else:
        if first is None or log != first["uid"] or ticket != {"key": first["key"], "uid": first["uid"]}:
            raise stop()
        who = {"key": first["key"], "uid": first["uid"]}
        if prev_head is None or ev.get("based_on") != prev_head:
            raise stop()  # a foreign event in between, or not the head of the event this signer signed at k-1
        if kind == "approve":
            _check_gate(intent, plan, k, prepared, material, who, done or {}, person)
        else:
            _check_content(kind, want, material, plan, k, stop)
    if canon.dumps(got) != canon.dumps(want):
        raise stop()
    return who


def _check_content(
    kind: str,
    want: Mapping[str, Any],
    material: Mapping[str, Any],
    plan: list[dict[str, Any]],
    k: int,
    stop: Callable[[], Aborted],
) -> None:
    """The material of a ticket-edit step (a section, a criterion, people) says nothing the intent does not."""
    texts = material.get("texts")
    sid = next(iter(want.get("sections") or {}), None)
    if sid is not None and texts != {sid: plan[k]["args"]["message"]}:
        raise stop()
    if sid is None and texts:
        raise stop()
    before = material.get("before") or {}
    # the host's "current values" (signer.check ties each to base_rev); nothing the intent does not touch
    carries_sequence = "sequence" in material  # only the first request of a sequence carries the intent
    if (
        not isinstance(before, Mapping)
        or not set(before) <= set(want.get("base_rev") or {})
        or (carries_sequence and k > 0)
    ):
        raise stop()


def _open_keys(kind: str) -> frozenset[str]:
    if kind in ("approve", "grant"):
        from . import seqquick

        return seqquick.open_keys(kind)
    return frozenset()


def _check_gate(
    intent: Mapping[str, Any],
    plan: list[dict[str, Any]],
    k: int,
    prepared: Mapping[str, Any],
    material: Mapping[str, Any],
    who: Mapping[str, str],
    done: Mapping[int, str],
    person: str,
) -> None:
    """A gate approval binds exactly the content this signer signed: the gate input is rebuilt from the intent and
    compared; its hash (and the policy hash) is recomputed by ``signer.check``. Nothing of it is taken on the host's
    word except what only the workspace knows (policy, people, generation numbers)."""
    from . import seqquick

    gate = plan[k]["args"]["gate"]
    first_approval = next((i for i, s in enumerate(plan) if s["kind"] == "approve"), None)
    prior = done.get(first_approval) if gate == "plan" and first_approval is not None else None
    expect = seqquick.gate_expect(
        intent, gate, workspace_id=str(prepared.get("workspace_id")), uid=who["uid"], requirements_approval=prior
    )
    if (
        material.get("kind") != "gate"
        or material.get("gate") != gate
        or (gate == "plan" and prior is None)
        or not seqquick.check_gate_input(expect, material.get("input"), material.get("texts"))
    ):
        raise Aborted(CHANGED)


_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_GRANT = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
GRANT_SKEW = 600  # the host's clock and this one may differ by this many seconds


def _epoch(stamp: str) -> int:
    import calendar
    import time

    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _check_grant(ev: Mapping[str, Any], intent: Mapping[str, Any], now: float | None) -> None:
    """The three values a grant leaves to the host: its id, and its times, which must span exactly the hours the person
    reviewed and start about now (a grant that starts in the past or the future is not the one that was shown)."""
    import time

    try:
        issued, expires = ev["issued_at"], ev["expires_at"]
        if not (isinstance(ev["grant"], str) and _GRANT.fullmatch(ev["grant"])):
            raise Aborted(CHANGED)
        if not (
            isinstance(issued, str)
            and isinstance(expires, str)
            and _TIME.fullmatch(issued)
            and _TIME.fullmatch(expires)
        ):
            raise Aborted(CHANGED)
        hours = intent["agent"]["hours"]
        t = time.time() if now is None else now
        if _epoch(expires) - _epoch(issued) != hours * 3600 or abs(_epoch(issued) - t) > GRANT_SKEW:
            raise Aborted(CHANGED)
    except (KeyError, ValueError):
        raise Aborted(CHANGED) from None


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
    if "log" in meta:  # a step on the workspace log (the add-on sequence): it names the log, not a ticket
        if (
            set(meta) != {"step", "of", "digest", "prev", "log"}
            or meta["log"] != "workspace"
            or not 1 <= step < total <= MAX_STEPS
            or op != "addon.grant"
        ):
            raise SequenceInvalid(
                "a workspace step names its number, the digest, the workspace log and the event before"
            )
        if not isinstance(meta["prev"], str) or not _ULID.fullmatch(meta["prev"]):
            raise SequenceInvalid("prev is an event id")
        return dict(meta)
    if set(meta) != {"step", "of", "digest", "prev", "ticket"} or not 1 <= step < total <= MAX_STEPS:
        raise SequenceInvalid("a later step names its number, the digest, the ticket and the event before it")
    if not isinstance(meta["prev"], str) or not _ULID.fullmatch(meta["prev"]):
        raise SequenceInvalid("prev is an event id")
    if not isinstance(meta["ticket"], str) or not _KEY.fullmatch(meta["ticket"]):
        raise SequenceInvalid("ticket is a ticket key")
    if op not in (
        "set",
        "section.set",
        "ac.add",
        "ac.edit",
        "task.add",
        "task.edit",
        "people.set",
        "approve",
        "grant",
        "artifact.add",
    ):
        raise SequenceInvalid("a later step is a set, section, file, criterion, task, people, approval or grant step")
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
    texts: dict[str, str]
    texts = dict(material.get("texts") or {})  # an edit's first step may already be a section
    for k in range(1, len(plan)):
        if plan[k]["kind"] in ("approve", "grant"):  # told in words (extra_lines); not an event of this review
            continue
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
            texts[sid] = plan[k]["args"]["message"]
    new_material: dict[str, Any] = {"kind": "payload", "ticket": material.get("ticket"), "review": []}
    if texts:
        new_material["texts"] = texts
    if intent["kind"] == ADDON and material.get("before"):
        new_material["before"] = copy.deepcopy(material["before"])  # the host's current settings (unsigned, labelled)
    if intent["kind"] == EDIT and "links" in intent:
        # the links the person reviewed: bound by the base_rev of the links event (the hash of exactly this value)
        new_material["before"] = {"ticket.links": copy.deepcopy(intent["links"]["was"])}
    from orch.host import review

    new_material["review"] = [review.of(e, new_material) for e in events]
    out = copy.deepcopy(dict(prepared))
    out[_EVENTS] = events
    out["material"] = new_material
    return out


def _people_notes(intent: Mapping[str, Any]) -> list[str]:
    """What the people named in the review may do, said once per role (LOW-3 of the #1070 review)."""
    ppl = intent.get("people") or {}
    out = []
    if "reviewers" in ppl:
        out.append("Reviewers may approve the Verify step and accept acceptance checks.")
    if "assignees" in ppl:
        out.append("Assignees do the work on the ticket and count as its workers.")
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
    if intent["kind"] == ADDON:  # no ticket: the workspace's settings and one add-on
        return _addon_content(content, intent, n)
    key = prepared["material"]["ticket"]["key"]
    # the key is the host's word: said so right next to it (invariant 1), with the end of the signed id
    key = f"{signcontent.one(key)} (key per host; signed id …{content['ticket']['uid_tail']})"
    if intent["kind"] == QUICK:
        content["headline"] = f"Start task {key}: {signcontent.one(intent['title'])}"
        content["subline"] = "creates the task and approves its requirements and plan"
        content.setdefault("notes", []).extend(extra_lines(intent))
    elif intent["kind"] == EDIT:
        content["headline"] = f"Edit {key}"
        content["subline"] = "its text, acceptance checks, tasks, repositories and people, as shown below"
        notes = content.setdefault("notes", [])
        if intent["sections"]:
            names = ", ".join(signcontent.one(sid.replace("_", " ").capitalize()) for sid in intent["sections"])
            notes.append(f"Replaces the whole current text of: {names}.")
        for aid, text in intent.get("acceptance", {}).get("edit", {}).items():
            was = next((a["text"] for a in intent["acceptance"]["was"] if a["id"] == aid), "")
            notes.append(f"{signcontent.one(aid)}: {signcontent.one(was)} -> {signcontent.one(text)}")
        for line in _task_lines(intent, signcontent.one, notes=True):
            notes.append(line)
        if "links" in intent:
            notes.append("Repositories: the links shown below replace the repositories and branches now on the ticket.")
        if "owner" in intent.get("people", {}):
            notes.append("The owner is set last. After that you may no longer be able to change this ticket yourself.")
        notes.extend(_people_notes(intent))
        notes.append(
            "Only the ticket as it was when you opened it is edited. If anyone changes it meanwhile, signing "
            "stops and nothing more is signed."
        )
    else:
        content.setdefault("notes", []).extend(extra_lines(intent))
        count = len(intent["sections"])
        # one headline for the whole review; the text is a subline (not one clause per signature)
        content["headline"] = f"Create {key}: {signcontent.one(intent['title'])}"
        parts = []
        if count:
            parts.append(f"its text ({count} section{'s' if count != 1 else ''})")
        if intent.get("acceptance"):
            parts.append(f"{len(intent['acceptance'])} acceptance check{'s' if len(intent['acceptance']) != 1 else ''}")
        if intent.get("people"):
            parts.append("its people")
        if parts:
            content["subline"] = "Includes " + ", ".join(parts)
        elif intent["set"]:
            content["subline"] = "Includes its details"
        if "owner" in intent.get("people", {}):
            content.setdefault("notes", []).append(
                "The owner is set last. After that you may no longer be able to change this ticket yourself."
            )
        content.setdefault("notes", []).extend(_people_notes(intent))
    if intent.get("images"):
        # the browser compares the digest of the bytes IT holds with this one, and shows them only when they match
        content["images"] = [
            {"name": i["name"], "sha256": i["sha256"], "bytes": i["bytes"], "alt": signcontent.one(i["alt"])}
            for i in intent["images"]
        ]
        content.setdefault("notes", []).append(
            f"Adds {len(intent['images'])} screenshot{'s' if len(intent['images']) != 1 else ''}. "
            "The dashboard shows each one from the file you pasted and checks its digest against the one signed."
        )
    content.setdefault("notes", []).append(
        f"One confirmation signs all {n} steps. If the ticket changes meanwhile, "
        "signing stops and what is already signed stays."
    )
    return content


def _addon_content(content: dict[str, Any], intent: Mapping[str, Any], n: int) -> dict[str, Any]:
    """The review of turning on an add-on that needs settings: the settings in words, then the grant (both are rows of
    the events, built by :func:`signcontent.build`), and what happens if signing stops between the two."""
    from orch.addons.settings_needs import TITLES

    content["headline"] = f"Turn on {TITLES[intent['name']]}"
    content["subline"] = "its settings are signed first, then the add-on is turned on"
    notes = content.setdefault("notes", [])
    notes.append(
        "Settings are signed first, then the add-on is turned on. If signing stops in between, "
        "the settings stay and the add-on stays off."
    )
    notes.append(
        f"One confirmation signs all {n} steps. If the workspace changes meanwhile, "
        "signing stops and what is already signed stays."
    )
    return content


def technical_lines(intent: Mapping[str, Any]) -> list[str]:
    """The technical part of the full review for a quick task: what the host fills in and the signer checks."""
    if intent["kind"] != QUICK:
        return []
    return [
        "technical: the plan approval's prior is the requirements approval of this run (checked by the signer); "
        "its generation (prior.requirements.gen) is set by the host when that step is prepared (per host, not signed "
        "by you until you confirm)",
        "technical: a new quick task carries no add-on fields or packages (checked by the signer)",
    ]


def extra_lines(intent: Mapping[str, Any], now: float | None = None) -> list[str]:
    """The steps that are not events of the plain review, in words (the plain view and the full review show them):
    the two gate approvals, which bind the content shown, and the grant, honestly about its reach (Opus change 5).
    A new ticket with an agent part: the grant sentence, and that the start itself is NOT signed (U4, I5)."""
    from . import seqquick

    if intent["kind"] == KIND:
        if "agent" not in intent:
            return []
        return [
            seqquick.grant_line(intent["agent"], now),
            "Not signed: the agent start. After the ticket exists, the dashboard may start an agent that uses this "
            "access; that start is not part of this confirmation. Nothing here approves the requirements or the plan.",
        ]
    if intent["kind"] != QUICK:
        return []
    out = [
        "Approves the requirements as written above, and the plan as written above "
        "(the signer checks that each approval covers exactly this text).",
        "The task still ends with your own verdict: nothing closes it for you.",
    ]
    if intent["agent"] is not None:
        out.append(seqquick.grant_line(intent["agent"], now))
    return out


def _link_changes(lk: Mapping[str, Any]) -> list[str]:
    """One plain line per repository whose link or branch changes: ``name: not linked -> linked, branch b``."""

    def word(repos: list[str], branches: Mapping[str, str], name: str) -> str:
        if name not in repos:
            return "not linked"
        return f"linked, branch {branches[name]}" if name in branches else "linked, no branch"

    was = lk["was"]
    names = sorted(set(was["repos"]) | set(lk["repos"]))
    out = []
    for n in names:
        old, new = word(was["repos"], was["branches"], n), word(lk["repos"], lk["branches"], n)
        if old != new:
            out.append(f"{n}: {old} -> {new}")
    return out


def _task_lines(intent: Mapping[str, Any], clean: Callable[[Any], str], *, notes: bool = False) -> list[str]:
    from orch.host.signcontent import one

    tasks = intent.get("tasks", {})
    old = {t["id"]: t for t in tasks.get("was", [])}

    def show(value: Any) -> str:
        return clean(one(value))

    def word(key: str, value: Any) -> str:
        if key == "proves":
            return ", ".join(value) if value else "none"
        return value if value is not None else "none"

    lines = []
    for tid, change in tasks.get("edit", {}).items():
        previous = old.get(tid, {})
        for key, value in change.items():
            before = previous.get(key)
            if key == "verify":
                before = before.get("cmd") if isinstance(before, Mapping) else None
            prefix = f"Task {show(tid)}" if notes else f"task {show(tid)} {key}"
            if notes and key == "verify":
                lines.append(f"{prefix} now verifies with: {show(word(key, value))}")
            else:
                if notes and key == "proves":
                    prefix += " proves"
                lines.append(f"{prefix}: {show(word(key, before))} -> {show(word(key, value))}")
    for task in tasks.get("add", []):
        prefix = "Task added" if notes else "task added"
        lines.append(
            f"{prefix}: {show(task['text'])} (verify: {show(word('verify', task.get('verify')))}; "
            f"proves: {show(word('proves', task['proves']))})"
        )
    return lines


def _image_lines(intent: Mapping[str, Any], clean: Callable[[Any], str]) -> list[str]:
    from orch.host.signcontent import one

    return [
        f"screenshot: {clean(i['name'])}, {i['bytes']} bytes, {clean(i['sha256'])} ({clean(one(i['alt']))})"
        for i in intent.get("images", ())
    ]


def intent_lines(intent: Mapping[str, Any], clean: Callable[[Any], str]) -> list[str]:
    """The intent in plain words, for a terminal prompt that has no SignContent."""
    from orch.host.signcontent import one

    if intent["kind"] == ADDON:
        from . import seqaddon

        return seqaddon.lines(intent, clean)
    if intent["kind"] == QUICK:
        lines = [f"title: {clean(one(intent['title']))}", "description:"]
        lines += ["| " + clean(x) for x in intent["description"].split("\n")]
        lines += [
            f"check: {clean(one(c['text']))}" + (f" (checked by {clean(one(c['cmd']))})" if c["cmd"] else "")
            for c in intent["checks"]
        ]
        return lines
    if intent["kind"] == EDIT:
        lines = [f"edit: {clean(one(intent['ticket']['key']))}", f"type: {clean(intent['ticket_type'])}"]
        for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
            lines.append(f"section {clean(sid)} (replaces the current text):")
            lines += ["| " + clean(line) for line in intent["sections"][sid]["text"].split("\n")]
        ac = intent.get("acceptance")
        if ac:
            old = {x["id"]: x["text"] for x in ac["was"]}
            lines += [
                f"acceptance {clean(a)}: {clean(one(old.get(a, '')))} -> {clean(one(t))}" for a, t in ac["edit"].items()
            ]
            lines += [f"acceptance added: {clean(one(t))}" for t in ac["add"]]
        lines += _task_lines(intent, clean)
        lk = intent.get("links")
        if lk:
            lines += [f"repository: {clean(one(x))}" for x in _link_changes(lk)]
        for role, ch in (intent.get("people") or {}).items():
            lines += [f"{clean(role)} add: {clean(who)}" for who in ch["add"]]
            lines += [f"{clean(role)} remove: {clean(who)}" for who in ch["remove"]]
        return lines + _image_lines(intent, clean)
    lines = [f"title: {clean(one(intent['title']))}", f"type: {clean(intent['ticket_type'])}"]
    lines += [f"{clean(k)}: {clean(one(v))}" for k, v in intent["set"].items()]
    for sid in sorted(intent["sections"], key=SECTION_ORDER.index):
        lines.append(f"section {clean(sid)}:")
        lines += ["| " + clean(line) for line in intent["sections"][sid].split("\n")]
    lines += [f"acceptance: {clean(one(t))}" for t in intent.get("acceptance", ())]
    lines += _image_lines(intent, clean)
    for role, who in (intent.get("people") or {}).items():
        lines += [f"{clean(role)}: {clean(one(x))}" for x in ([who] if role == "owner" else who)]
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
        asked again. A live unfinished one is not: another run of the same review holds it (the check and the write
        are one step under an exclusive lock on the session directory)."""
        import fcntl

        fd = self._dir(workspace_id, create=True)
        assert fd is not None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = self.get(workspace_id, digest)
            if held is not None and held["aborted"] is None and not _complete(held):
                raise SessionError("another run of this same review is in progress")
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
        finally:
            os.close(fd)  # closing releases the lock

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


def _complete(rec: Mapping[str, Any]) -> bool:
    return len(rec["steps"]) == rec["total"] and all(s["state"] == "landed" for s in rec["steps"])


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
    #: (a step that adds a pasted screenshot also gets ``files={"path": bytes}``; the port uploads inline or as a blob)
    prepare: Callable[..., dict[str, Any]]
    #: the signer's own checks of a prepared request (:func:`orch.host.signer.check`), before anything is signed
    check: Callable[[dict[str, Any]], None]
    #: ``prepared -> signed events`` (the custody backend; one unlock for the whole sequence)
    sign: Callable[[dict[str, Any]], list[dict[str, Any]]]
    #: ``(request_id, events, idem) -> result`` (``human.submit``)
    submit: Callable[[str, list[dict[str, Any]], str], dict[str, Any]]
    new_idem: Callable[[], str]
    #: ``ticket key -> {"seq", "head", "tail"}``: the log head the host reports now (``ticket.get``'s ``doc.head``) and
    #: the last entry of its log
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
        now: Callable[[], float] = time.time,
        images: Mapping[str, bytes] | None = None,
    ) -> None:
        self.now = now
        #: R3 part B: the bytes of the pasted screenshots by name, held by the signer (never taken from the host)
        self.images = dict(images or {})
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
        if _complete(rec):
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
        self._check_images()
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
        workspace = self.intent["kind"] == ADDON  # a sequence on the workspace log: no ticket, no head read
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
                        resumable=True,
                    ) from None
            results.append(res)
            prev_id, prev_head = events[-1]["id"], self._land(rec, k, res)
            if self.on_step:
                self.on_step(k + 1, self.total)
        requests = [st["request_id"] for st in rec["steps"]]
        if workspace:
            return {
                "key": None, "uid": None, "log": "workspace", "results": results, "landed": self.total,
                "requests": requests,
            }  # fmt: skip
        assert who is not None
        return {"key": who["key"], "uid": who["uid"], "results": results, "landed": self.total, "requests": requests}

    def _check_images(self) -> None:
        """Before anything is signed: every screenshot of the intent is held here, is the file the person reviewed
        (digest and size of the intent) and passes the picture check (:mod:`orch.host.imagecheck`)."""
        from . import imagecheck

        wanted = self.intent.get("images", ())
        if not wanted:
            if self.images:
                raise Aborted("screenshots were sent that no section shows, review again")
            return
        if set(self.images) != {i["name"] for i in wanted}:
            raise Aborted(
                "The screenshots are not here. Screenshots are signed with a click in the dashboard, review again"
            )
        for i in wanted:
            data = self.images[i["name"]]
            if canon.artifact_digest(data) != i["sha256"] or len(data) != i["bytes"]:
                raise Aborted("a screenshot is not the file you reviewed, review again")
            try:
                imagecheck.check(data)
            except imagecheck.ImageRefused as exc:
                raise Aborted(f"{i['name']}: {exc}") from None

    def _land(self, rec: dict[str, Any], k: int, res: Mapping[str, Any]) -> str | None:
        """The step landed: read the head the host reports and require it to be the head of the event this signer just
        signed (its ``seq`` is the submit's), so the next step chains to it. A foreign event already on top aborts."""
        head: str | None = None
        rec["steps"][k].update(state="landed", head=None)  # it landed: counted, whatever the head read says
        if k < self.total - 1 and self.intent["kind"] != ADDON:  # the workspace chain is the host's check (AE6)
            try:
                got = self.port.head(rec["ticket"]["key"])
            except Exception as exc:  # noqa: BLE001
                raise self._abort(rec, f"the ticket could not be read after step {k + 1} ({_why(exc)})") from None
            seq, mine = res.get("seq"), rec["steps"][k]["signed"][-1]
            tail = got.get("tail")
            # the tail of the log is the event this signer just signed: its seq, type and actor (read from the log, not
            # the host's say-so about a head). The head HASH itself cannot be recomputed here: it covers the host's own
            # stamps and signature, which no read op hands to a person client (follow-up).
            if (
                type(seq) is not int
                or got.get("seq") != seq
                or not isinstance(got.get("head"), str)
                or not isinstance(tail, Mapping)
                or tail.get("seq") != seq
                or tail.get("type") != mine["type"]
                or (tail.get("actor") or {}).get("id") != mine["actor"]["id"]
                or (tail.get("actor") or {}).get("device") != mine["actor"]["device"]
            ):
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
            elif self.intent["kind"] == ADDON:
                op, args = step_args(self.intent, k, None)
                meta = {"step": k, "of": self.total, "digest": self.digest, "prev": prev_id, "log": "workspace"}
                prepared = self.port.prepare(op, args, meta)
                self.port.check(prepared)
            else:
                assert who is not None
                op, args = step_args(self.intent, k, who["key"])
                meta = {"step": k, "of": self.total, "digest": self.digest, "prev": prev_id, "ticket": who["key"]}
                step = steps(self.intent)[k]
                if step["kind"] == "image":  # the signer uploads the file it holds (its digest is the intent's)
                    prepared = self.port.prepare(op, args, meta, files={"path": self.images[step["args"]["name"]]})
                else:
                    prepared = self.port.prepare(op, args, meta)
                self.port.check(prepared)
            done = {st["k"]: st["signed"][-1]["id"] for st in rec["steps"] if st["state"] == "landed"}
            found = check_step(
                self.intent,
                k,
                prepared,
                person=self.person,
                first=who if k else None,
                prev_head=prev_head,
                done=done,
                now=self.now(),
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
