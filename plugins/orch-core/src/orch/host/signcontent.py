"""The plain view of a signing request (B6, owner decision E of 16 Oct 2026): a headline, the content formatted like the
ticket page, and (in the terminal) the confirm prompt; the full dump stays one key away.

``build`` is a **pure function of the request the signer already checked** (``events`` and ``material``): every word it
shows is taken from those, never from host text outside them, so the content shown is the content that is signed. It
adds nothing to the material and changes no hash; the fingerprint, the review digest and the signed bytes are exactly
what they were.

**The invariant (security review of 16 Oct 2026).** The plain view shows every signed payload key that carries authority
or identity. A key may stay out of it only if it is in :data:`TECHNICAL_KEYS`, an explicit, reviewed list of hashes,
positions and attestations (they are in the details). For every event type the renderer names the keys it shows
(:data:`SHOWN`); a payload, gate input or task key that is neither shown nor technical, or an event type this module
does not know, raises :class:`PlainUnavailable` and the caller falls back to the full review (no plain view at all).
``tests/host/test_sign_plain.py`` enumerates every event type a person can sign and every property of its schema, and
fails when a key is added without being classified here.

The shape (``SignContent``) is the contract the dashboard reads (batch-6.md "Contract")::

    {headline, ticket?: {key, title}, sections?: [{id, title, text}], acceptance?: [{id, text, checked?}],
     tasks?: [{n, id, title, verify?: [str]}], fields_changed?: [{field, before, after, before_note?}],
     fields_title?: str, notes?: [str], title?: str}
    title = the headline without the host-key / signed-id qualifier, for a dialog title (U3); the qualifier stays in
    ``ticket`` (key_note, uid_tail), which the dashboard shows under "Technical details"; ``headline`` is unchanged
    ticket = {key, title, uid_tail, key_note, new}
    // key_note: the key is the host's word; uid_tail is the signed id's end; new: this review creates the ticket

``before_note`` is set when ``before`` is the host's word and not checked against anything signed. The ticket title is
shown only when a signed event states it (a ``ticket.created``): a gate does not bind the title, so ``ticket.title`` is
``""`` for a gate request.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

__all__ = ["DETAILS_ONLY", "SHOWN", "TECHNICAL_KEYS", "PlainUnavailable", "build", "clean_tree", "headline", "text"]


class PlainUnavailable(ValueError):
    """The request holds something the plain view does not know how to show: the caller shows the full review."""


#: payload keys that are envelope, stamps, hashes or attestations: never shown in the plain view, always in the details
TECHNICAL_KEYS = frozenset(
    {
        # envelope and stamps
        "v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig", "seq", "at", "prev", "ws_seq",
        "host_sig",
        # hashes and attestations the details carry
        "gate_gen", "hash", "policy_hash", "base_rev", "secret_hash",
        # C10: the id of the grant.requested a host-actor grant.issued answers; a person never signs it
        "request",
    }
)  # fmt: skip

#: per event type, the payload keys the plain view shows (in a section, a list or a field row)
SHOWN: dict[str, frozenset[str]] = {
    "addon.disabled": frozenset({"name"}),
    "addon.purged": frozenset({"name"}),
    "addon.granted": frozenset(
        {"name", "version", "package_sha256", "capabilities", "artifact_kinds", "fields", "sections"}
    ),
    "artifact.added": frozenset({"name", "kind", "label", "ac", "task", "ref", "addon", "bytes", "sha256"}),
    "artifact.replaced": frozenset({"name", "kind", "label", "ac", "task", "replaces", "bytes", "sha256"}),
    "device.added": frozenset({"device", "cert"}),
    "gate.approved": frozenset({"gate", "source_sha"}),
    "gate.changes_requested": frozenset({"gate", "text"}),
    "verdict.given": frozenset({"outcome", "text", "source_sha"}),
    "hub.joined": frozenset({"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases"}),
    "hub.left": frozenset({"hub_id", "binding_seq"}),
    "grant.issued": frozenset({"grant", "scope", "verbs", "label", "hours", "issued_at", "expires_at"}),
    "grant.revoked": frozenset({"grant", "reason"}),
    "log.added": frozenset({"text"}),
    "member.added": frozenset({"person", "name", "role", "pk_pub", "device_cert"}),
    "member.removed": frozenset({"person"}),
    "role.changed": frozenset({"person", "role"}),
    "people.changed": frozenset({"role", "add", "remove"}),
    "policy.changed": frozenset({"gates"}),
    "question.answered": frozenset({"question", "option", "text"}),
    "question.asked": frozenset({"qid", "question"}),
    "settings.changed": frozenset({"set"}),
    "task.done": frozenset({"task", "receipt", "log", "text"}),
    "ticket.closed": frozenset({"resolution", "duplicate_of", "text"}),
    "ticket.created": frozenset({"key", "ticket_type", "title", "owner"}),
    "ticket.moved": frozenset({"from", "to", "reason"}),
    "ticket.reopened": frozenset({"text"}),
    "ticket.updated": frozenset({"set", "sections"}),
}

#: K4: keys the plain view says in WORDS or leaves to the technical details (``d``, the full review, which carries every
#: signed key). Only for the ordinary grant (scope ``workable``, verbs ``agent``); any other value shows them as rows.
DETAILS_ONLY: dict[str, frozenset[str]] = {
    "grant.issued": frozenset({"grant", "scope", "verbs", "issued_at", "expires_at"}),
    "grant.revoked": frozenset({"grant"}),
}

#: the keys of a gate input the plain view shows, and the ones that are hashes; of its ``fields``, a task, an
#: acceptance criterion, a policy, an artifact and a receipt, the ones it shows
_INPUT_SHOWN = frozenset({"fields", "tasks", "artifacts", "receipts", "source_sha", "prior"})
_INPUT_TECHNICAL = frozenset(
    {"workspace_id", "uid", "gate", "schema", "hash_v", "sections", "policy_hash", "people_hash", "addon_packages"}
)
_FIELDS_SHOWN = frozenset({"ticket_type", "size", "acceptance", "links", "addons"})
_TASK_SHOWN = frozenset({"id", "text", "verify", "proves"})
_AC_SHOWN = frozenset({"id", "text"})
_POLICY_SHOWN = frozenset({"approvers", "count", "not", "applies", "independent"})
_ARTIFACT_SHOWN = frozenset({"kind", "digest", "ac", "task"})
_RECEIPT_SHOWN = frozenset({"event", "repo", "commit", "exit"})


def _s(value: Any) -> str:
    return value if isinstance(value, str) else canon.dumps(value).decode("utf-8")


def span(hours: Any) -> str:
    """B8: a grant length in the words a person uses: ``3 days`` for a whole number of days, else ``N hours``."""
    if type(hours) is not int or hours < 1:
        return one(hours)
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" + ("" if days == 1 else "s")
    return f"{hours} hour" + ("" if hours == 1 else "s")


def one(value: Any) -> str:
    """The ONE helper every single-line value goes through (security review: a newline inside a title, a verify command,
    an acceptance text or a field must not fake structure such as a second task). Each line break is made visible as
    `` ⏎ ``; a block (a section's text) is rendered by :func:`text` under a ``| `` mark on every line instead."""
    return " ⏎ ".join(_s(value).replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _line1(value: Any, limit: int = 80) -> str:
    one = " ".join(_s(value).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _title(sid: str) -> str:
    return sid.replace("_", " ").replace(".", " ").capitalize()


def _show(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return one(value)
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return one(", ".join(value)) if value else "(none)"
    return one(_s(value))


def _short(digest: Any) -> str:
    """The first 12 hex digits of a ``sha256:`` digest."""
    return _s(digest).removeprefix("sha256:")[:12]


def _fp(value: Any) -> str:
    """A short fingerprint of a key or certificate: the first 12 hex of the sha256 of its canonical text."""
    return hashlib.sha256(_s(value).encode("utf-8")).hexdigest()[:12]


def _check_keys(
    where: str, got: Mapping[str, Any], shown: frozenset[str], technical: frozenset[str] = frozenset()
) -> None:
    extra = set(got) - shown - technical
    if extra:
        raise PlainUnavailable(f"the plain view does not know {where} {sorted(extra)}")


def _ticket(material: Mapping[str, Any], events: list[Mapping[str, Any]]) -> dict[str, str] | None:
    t = material.get("ticket")
    if not isinstance(t, Mapping) or not isinstance(t.get("key"), str):
        return None
    title = ""
    for ev in events:
        if ev.get("type") == "ticket.created" and isinstance(ev.get("title"), str):
            title = ev["title"]
    tail = _s(t.get("uid"))[-8:]
    return {
        "key": t["key"],
        "title": title,
        "uid_tail": tail,
        "key_note": f"key per host, not proven; the ticket's signed id ends …{tail}",
    }


def _ref(ticket: Mapping[str, str] | None) -> str:
    """The ticket as the headline names it: the host's key (not proven), the title only when a signed event states it,
    and the tail of the ticket's signed id (the key is the host's word, the id is what the signature names)."""
    if ticket is None:
        return "the workspace"
    name = f"{ticket['key']} · {one(ticket['title'])}" if ticket["title"] else ticket["key"]
    return f"{name} (key per host; signed id …{ticket['uid_tail']})"


_SHARE_SIZE = r"\d+(?:\.\d+)? ?[A-Za-z]+"
_SHARE_FILE = re.compile(r"- (.+)  \((" + _SHARE_SIZE + r"), sha256 ([0-9a-f]{16})\)")
_SHARE_HEAD = re.compile(r"Files \((\d+), (" + _SHARE_SIZE + r")\):")
_SHARE_TICKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _share_summary(text: str) -> dict[str, Any] | None:
    """The plain lead of a share approval (the PF1 review): files, who can open them, how long, where they go, read
    from the SIGNED text only (``orch.publishclient.approval.approval_text``). It is a summary that never replaces the
    text: the dashboard shows the whole signed text beside it. ``None`` (no summary, the text alone) as soon as one
    line is not exactly what that function writes (its own name, origin, staged-id, digest, day and view patterns are
    reused), so an odd value can never be summarised into something it is not."""
    from orch.publishclient import approval as ap

    lines = text.split("\n")
    if len(lines) < 11 or lines[0] != ap.MARK:
        return None
    ticket = re.fullmatch(r"Ticket: (\S+)", lines[1])
    prepared = re.fullmatch(r"Prepared by: (an agent|you)", lines[2])
    secret = lines[3] == "Link: a secret link: anyone who has the link can open the files"
    once = re.fullmatch(r"Link: a show-once link: it opens at most (\d+) times?", lines[3])
    works = re.fullmatch(r"Works for: (\d+) days? from the moment the link is made", lines[4])
    server = re.fullmatch(r"The files are sent to: (\S+)", lines[5])
    share = re.fullmatch(r"The link opens at: (\S+)", lines[6])
    head = _SHARE_HEAD.fullmatch(lines[7])
    if not (ticket and prepared and (secret or once) and works and server and share and head):
        return None
    if not (_SHARE_TICKET.fullmatch(ticket.group(1)) and int(works.group(1)) in ap.DAYS):
        return None
    if once and (int(once.group(1)) not in ap.VIEWS or lines[3] != _views_line(int(once.group(1)))):
        return None
    nd = int(works.group(1))
    if lines[4] != f"Works for: {nd} day{'' if nd == 1 else 's'} from the moment the link is made":
        return None
    if not (ap._ORIGIN.fullmatch(server.group(1)) and ap._ORIGIN.fullmatch(share.group(1))):
        return None
    if not (
        re.fullmatch(r"Staged copy: " + ap._STAGED.pattern, lines[-2])
        and re.fullmatch(r"Manifest: sha256:" + ap._HEX64.pattern, lines[-1])
    ):
        return None
    rows = lines[8:-2]
    names = []
    for row in rows:
        m = _SHARE_FILE.fullmatch(row)
        if m is None or ap.NAME.fullmatch(m.group(1)) is None:
            return None
        names.append(m.group(1))
    if len(rows) != int(head.group(1)) or not 1 <= len(rows) <= ap.LIMITS["files"]:
        return None
    n, days = int(head.group(1)), int(works.group(1))
    who = "Anyone who has the link can open the files"
    if once:
        views = int(once.group(1))
        who += f", at most {views} time{'' if views == 1 else 's'}"
    out = [
        f"Files: {n} ({head.group(2)}): " + ", ".join(names),
        f"Who can open them: {who}.",
        f"The link works for {days} day{'' if days == 1 else 's'} from the moment it is made.",
        f"The files are sent to {server.group(1)}; the link opens at {share.group(1)}.",
        f"Prepared by {prepared.group(1)}. From ticket {ticket.group(1)} (key per host).",
    ]
    return {"title": "Share these files", "lines": out}


def _views_line(views: int) -> str:
    return f"Link: a show-once link: it opens at most {views} time{'' if views == 1 else 's'}"


_GRANT_INFO = frozenset({"label", "scope", "expires_at", "person"})


def _grant_words(ev: Mapping[str, Any], ctx: _Ctx) -> tuple[str, bool]:
    """Which access an event takes back, in words and by the host's word (``material["grants"]``, labelled "per host,
    not signed" where it is shown):
    ``("agent access for this Mac's runner, until Sun 00:37", True)`` for the person's own
    runner access, ``(.., False)`` for any other own access. The id is never in the words (it is in the technical
    details). Anything that cannot be said distinctly (no host word, a malformed one, an access of another person, no
    end time) raises :class:`PlainUnavailable`: the caller then shows the full review, with the ids."""
    gid = ev.get("grant")
    info = ctx.grants.get(gid) if isinstance(ctx.grants, Mapping) and type(gid) is str else None
    if not isinstance(info, Mapping) or set(info) != _GRANT_INFO or not all(type(v) is str for v in info.values()):
        raise PlainUnavailable("the host did not say which access is taken back")
    until = local_time(info["expires_at"])
    if until is None:
        raise PlainUnavailable("the access to be taken back has no usable end time")
    if info["person"] != (ev.get("actor") or {}).get("id"):
        raise PlainUnavailable("the access to be taken back is not the signer's own")
    if info["label"] == "runner" and info["scope"] == "workable":
        return f"agent access for this Mac's runner, until {until}", True
    label = f' "{one(info["label"])}"' if info["label"] else ""
    return f"agent access{label} (tickets: {one(info['scope'])}), until {until}", False


_EXTEND_WARNING = "Running agents will stop. New agents use the extended access (per host, not signed)."


def _extend_summary(events: list[Mapping[str, Any]], ctx: _Ctx) -> dict[str, Any] | None:
    """The plain lead of an Extend (the ordinary runner grant plus the take-back of the access it replaces, B8).
    Only for exactly that shape; anything else keeps the generic review (nothing here may claim less than is signed)."""
    kinds = [e.get("type") for e in events]
    if sorted(map(str, kinds)) != ["grant.issued", "grant.revoked"]:
        return None
    new = next(e for e in events if e.get("type") == "grant.issued")
    old = next(e for e in events if e.get("type") == "grant.revoked")
    hours = new.get("hours")
    if (
        old.get("reason") != "extended"
        or new.get("label") != "runner"
        or new.get("scope") != "workable"
        or new.get("verbs") != "agent"
        or type(hours) is not int
    ):
        return None
    until = local_time(new.get("expires_at"), hours)
    replaced, is_runner = _grant_words(old, ctx)
    if not is_runner:  # an Extend replaces the runner's own access; anything else is not what this summary says
        raise PlainUnavailable("the access an Extend takes back is not the runner's own access")
    lines = [
        f"New access: agents working for you, for {span(hours)}.",
        *([f"The new access ends {until} (your time)."] if until else []),
        f"The access it replaces is taken back when you sign. Per host, not signed: that is the {replaced}.",
        "Affected: agents working for you (per host, not signed).",
    ]
    # the headline is all the terminal's plain view prints first: it keeps the length and the end; the ids of the new
    # and the replaced access are in the technical details (the full review), never in the plain words
    head = f"Extend agent access for this Mac's runner, for {span(hours)}" + (
        f" (until {until}, your time)" if until else ""
    )
    return {"title": "Extend agent access", "lines": lines, "headline": head}


def _grant_short(value: Any) -> str:
    """A grant id as ``gr_…`` and its last 10 characters: enough to tell two grants apart on the screen; the full id is
    in the technical details. Two different ids with one short form are refused by the caller (:func:`_payload`)."""
    text = _s(value)
    return f"gr_…{text[-10:]}" if len(text) > 14 else text


def _row(out: dict[str, Any], field: str, after: Any, before: Any = None, note: str | None = None) -> None:
    row: dict[str, Any] = {"field": one(field), "before": _show(before), "after": _show(after)}
    if note and before is not None:
        row["before_note"] = note
    out.setdefault("fields_changed", []).append(row)


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def local_time(iso: Any, hours: Any = None) -> str | None:
    """K4/N1: a signed UTC ``YYYY-MM-DDTHH:MM:SSZ`` in this computer's time with the zone name (``Tue 23:45 CEST``; with
    the date when it is a week away or more). ``None`` when ``iso`` is not such a time (a placeholder): the caller then
    keeps the technical wording. English day names on purpose: the same bytes must render the same everywhere."""
    from datetime import UTC, datetime

    if type(iso) is not str:
        return None
    try:
        when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone()
    except ValueError:
        return None
    zone = when.tzname() or "UTC"
    day = (
        f"{_DAYS[when.weekday()]} {when.day} {_MONTHS[when.month - 1]}"
        if type(hours) is int and hours > 24
        else _DAYS[when.weekday()]
    )
    return f"{day} {when:%H:%M} {zone}"


def _when(iso: Any, hours: Any = None) -> Any:
    """A signed UTC time in this computer's time with the zone, the signed text after it (``Tue 23:45 CEST (2026-…Z)``);
    anything that is not such a time as it is."""
    local = local_time(iso, hours)
    return iso if local is None else f"{local} ({iso})"


_REPO_KEYS = frozenset({"path", "remote", "default_branch"})
#: the three things a repo entry holds and the label each has on its OWN row (never joined into one line, so no value
#: can pose as another field: a path that reads "/x · remote …" is still one value on the Folder row)
#: the words this view uses for "no value" and "removed": a signed value spelled like one would be ambiguous, so an
#: entry holding one is not plain and the generic whole-value row shows it instead
_REPO_MARKERS = ("(none)", "(removed)")
_REPO_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REPO_FIELDS = (("path", "folder"), ("remote", "remote"), ("default_branch", "main branch"))


def _repo_plain(entry: Any) -> bool:
    """Whether ``entry`` is a plain repo entry: a mapping with a non-empty ``path``, only the three known keys, all
    strings. Anything else is shown whole by the generic row (WYSIWYS: unknown fields fall back to the full view)."""
    return (
        isinstance(entry, Mapping)
        and isinstance(entry.get("path"), str)
        and bool(entry["path"])
        and not set(entry) - _REPO_KEYS
        and all(isinstance(v, str) and v not in _REPO_MARKERS for v in entry.values())
    )


def _repo_value(entry: Mapping[str, Any] | None, key: str) -> str:
    """One field of an entry as shown: the value through :func:`one`, an empty or missing value as ``(none)``."""
    value = None if entry is None else entry.get(key)
    return one(value) if isinstance(value, str) and value else "(none)"


def _repo_rows(out: dict[str, Any], value: Any, before: Any, ctx: Any) -> bool:
    """``settings.changed`` ``set.repos`` as labelled rows, a group per repo: its verdict (added, changed, removed) and
    then one row each for Folder, Remote and Main branch, old to new, every field every time (so an unchanged or empty
    one is visible, as ``(none)``). ``False`` when any entry is not plain: the generic row, with every byte, is then
    shown instead and nothing has been added."""
    if not isinstance(value, Mapping) or not value:
        return False
    before = before if isinstance(before, Mapping) else {}
    for name, entry in value.items():
        if (
            not isinstance(name, str)
            or not _REPO_NAME.fullmatch(name)
            or (entry is not None and not _repo_plain(entry))
        ):
            return False
        if before.get(name) is not None and not _repo_plain(before.get(name)):
            return False
    for name, entry in sorted(value.items()):
        old = before.get(name)
        who = f"repository {one(name)}"
        if entry is None:
            verdict = "removed (the folder and its files stay on disk)"
        else:
            verdict = "changed" if old is not None else "added"
        # the verdict comes from the host's unsigned `before`, so it is always labelled as such
        out.setdefault("fields_changed", []).append(
            {
                "field": one(who),
                "before": None if old is None else "listed",
                "after": verdict,
                "before_note": ctx.note(False),
            }
        )
        for key, label in _REPO_FIELDS:
            after = "(removed)" if entry is None else _repo_value(entry, key)
            was = _repo_value(old, key) if old is not None else "(none)"
            if entry is not None and old is not None and was == after:
                # E-4: a field that stays as it is says so (the host's unsigned word) and shows its value once, no
                # arrow; the value is still on the screen, so every signed byte is still shown
                _row(out, f"{who}: {label} (unchanged, per host)", after)
                continue
            _row(out, f"{who}: {label}", after, was, ctx.note(False))
    return True


def _publish_rows(out: dict[str, Any], value: Any, before: Any, ctx: Any) -> bool:
    """``settings.changed`` ``set.publish`` (P5): where shared files are sent and where a link opens, in words; ``None``
    removes the server. ``False`` (the generic row, every byte) when the value is anything else."""
    if value is None:
        _row(out, "Publish server", "(removed: nothing can be shared)", before, ctx.note(False))
        return True
    if not isinstance(value, Mapping) or set(value) != {"server", "share"}:
        return False
    old = before if isinstance(before, Mapping) else {}
    for key, label in (("server", "Publish: files are sent to"), ("share", "Publish: links open at")):
        if not isinstance(value[key], str):
            return False
        _row(out, label, value[key], old.get(key) or "(none)", ctx.note(False))
    out.setdefault("notes", []).append(
        "Files you share go to this server, signed by this workspace. Only a person can change it."
    )
    return True


_MODE_WORDS = {
    "work": "Do the work", "refine": "Write requirements and plan", "fix": "Fix what was sent back",
    "continue": "Continue", "context": "Read and answer",
}  # fmt: skip
_HARNESS_WORDS = {"claude-code": "Claude", "codex": "Codex"}


def _model_word(name: str | None) -> str:
    from orch.modelchoices import MODEL_LABELS

    return "Harness default" if name is None else MODEL_LABELS[name]


def _models_rows(out: dict[str, Any], value: Any, before: Any, ctx: Any) -> bool:
    """``settings.changed`` ``set.models`` (Model routing): one row per harness and mode, the model in words, ``Harness
    default`` where none is set. The new value replaces the whole table, so every mode of every harness is listed.
    ``False`` (the generic row, every byte) when the value is not a plain table of the closed lists."""
    from orch.modelchoices import MODEL_CHOICES

    def plain(v: Any) -> bool:
        return isinstance(v, Mapping) and all(
            h in MODEL_CHOICES
            and isinstance(per, Mapping)
            and all(m in _MODE_WORDS and per[m] in MODEL_CHOICES[h] for m in per)
            for h, per in v.items()
        )

    if value is not None and not plain(value):
        return False
    if before is not None and not plain(before):
        return False  # the host's current value is not a plain table: the generic row shows every byte
    new = value or {}
    old = before if plain(before) else {}
    for harness, hword in _HARNESS_WORDS.items():
        for mode, mword in _MODE_WORDS.items():
            after = new.get(harness, {}).get(mode)
            was = old.get(harness, {}).get(mode)
            _row(out, f"{hword}: {mword}", _model_word(after), _model_word(was), ctx.note(False))
    out.setdefault("notes", []).append(
        "Start agent uses these models for new agents. You still see the model before each start."
    )
    return True


def _icon_word(v: Any) -> str | None:
    """``Rocket, Pink`` for a plain pair of the closed lists, else ``None``."""
    from orch.wsicons import COLOUR_LABELS, ICON_LABELS

    if (
        not isinstance(v, Mapping)
        or set(v) != {"icon", "colour"}
        or not isinstance(v["icon"], str)
        or not isinstance(v["colour"], str)
        or v["icon"] not in ICON_LABELS
        or v["colour"] not in COLOUR_LABELS
    ):
        return None
    return f"{ICON_LABELS[v['icon']]}, {COLOUR_LABELS[v['colour']]}"


def _icon_rows(out: dict[str, Any], value: Any, before: Any, ctx: Any) -> bool:
    """``settings.changed`` ``set.icon``: the workspace icon in words (``Rocket, Pink``); ``None`` removes it. ``False``
    (the generic row, every byte) when the value, or the host's current one, is not a plain pair of the closed lists."""
    new = "(removed: default icon)" if value is None else _icon_word(value)
    if new is None:
        return False
    old = "(default icon)" if before is None else _icon_word(before)
    if old is None:
        return False
    _row(out, "Workspace icon", new, old, ctx.note(False))
    out.setdefault("notes", []).append(
        "The icon is shown beside the workspace name in menus. It never replaces the prefix."
    )
    return True


_LINK_KEYS = frozenset({"repos", "branches", "prs", "external"})
_LINK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _link_plain(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _LINK_KEYS
        and isinstance(value["repos"], list)
        and all(isinstance(r, str) and _LINK_NAME.fullmatch(r) for r in value["repos"])
        and isinstance(value["branches"], Mapping)
        and all(
            k in value["repos"] and isinstance(b, str) and b and "\n" not in b for k, b in value["branches"].items()
        )
    )


def _link_word(links: Mapping[str, Any], name: str) -> str:
    if name not in links["repos"]:
        return "not linked"
    branch = links["branches"].get(name)
    return f"linked, branch {one(branch)}" if branch else "linked, no branch"


def _links_rows(out: dict[str, Any], value: Any, before: Any, note: str | None) -> bool:
    """A ticket's ``links`` edit as one row per repository that changes (``repository X: not linked -> linked, branch
    Y``). ``False`` (the generic row, every byte) when either value is not plain, when the pull requests or external
    links change too, or when nothing about the repositories changes."""
    if not _link_plain(value) or not _link_plain(before):
        return False
    if value["prs"] != before["prs"] or value["external"] != before["external"]:
        return False
    rows = []
    for name in sorted(set(value["repos"]) | set(before["repos"])):
        old, new = _link_word(before, name), _link_word(value, name)
        if old != new:
            rows.append((name, new, old))
    if not rows:
        return False
    for name, new, old in rows:
        _row(out, f"repository {one(name)}", new, old, note)
    return True


def _grant_issued(ev: Mapping[str, Any], out: dict[str, Any]) -> str:
    """The grant review. The plain view of the ordinary grant (scope ``workable``, verbs ``agent``) is one sentence;
    scope, verbs, the grant id and the UTC times are in the technical details (``d``), which carry every signed field.
    Any other scope or verbs keeps the technical wording and rows: the plain sentence must never claim less than is
    signed (WYSIWYS)."""
    hours = ev.get("hours")
    plain = ev.get("scope") == "workable" and ev.get("verbs") == "agent" and type(hours) is int
    until = local_time(ev.get("expires_at"), hours) if plain else None
    _row(out, "who can use it", "agents working for you")
    if ev.get("label"):
        _row(out, "label", ev["label"])
    if plain and until is None:
        # the ordinary grant with its times not known yet (the setup review: they are filled in at signing): what agents
        # may do in words; scope, verbs and the grant id are in the technical details
        _row(out, "from", ev.get("issued_at"))
        _row(out, "until", ev.get("expires_at"))
        out.setdefault("notes", []).append("Scope, verbs and grant id are in the technical details.")
        return f"Agents may work on any ticket you can see, as you, for {span(hours)}"
    if not plain:
        _row(out, "scope", ev.get("scope"))
        _row(out, "verbs", ev.get("verbs"))
        _row(out, "from", _when(ev.get("issued_at"), hours))
        _row(out, "until", _when(ev.get("expires_at"), hours))
        _row(out, "grant id", ev.get("grant"))
        out.setdefault("notes", []).append(
            f"Agents can act for you for {span(hours)}, as far as the scope and verbs above allow."
        )
        return f"Give agents access for {span(hours)} (scope {_s(ev.get('scope'))}, verbs {_s(ev.get('verbs'))})"
    out.setdefault("notes", []).append("Scope, verbs, grant id and the exact times (UTC) are in the technical details.")
    return f"Agents may work on any ticket you can see, as you, for {span(hours)} (until {until}, your time)"


def _acceptance(items: Any) -> list[dict[str, Any]]:
    out = []
    for a in items or []:
        if not isinstance(a, Mapping):
            raise PlainUnavailable("an acceptance criterion is not an object")
        _check_keys("acceptance keys", a, _AC_SHOWN)
        out.append({"id": one(a.get("id")), "text": one(a.get("text")), "checked": False})
    return out


def _tasks(items: Any) -> list[dict[str, Any]]:
    out = []
    for n, t in enumerate(items or [], 1):
        if not isinstance(t, Mapping):
            raise PlainUnavailable("a task is not an object")
        _check_keys("task keys", t, _TASK_SHOWN)
        title = one(t.get("text"))
        if t.get("proves"):
            title += f" (proves {one(', '.join(_s(x) for x in t['proves']))})"
        task: dict[str, Any] = {"n": n, "id": one(t.get("id")), "title": title}
        verify = t.get("verify")
        if isinstance(verify, Mapping):
            _check_keys("verify keys", verify, frozenset({"cmd"}))
        elif verify is not None:
            raise PlainUnavailable("a task verify is not an object")
        cmd = verify.get("cmd") if isinstance(verify, Mapping) else None
        if cmd:
            task["verify"] = [one(cmd)]
        out.append(task)
    return out


def _task_words(t: Mapping[str, Any]) -> str:
    """One task on one line: its text, its verify command and the criteria it proves (every shown key is in it)."""
    out = one(t.get("text"))
    verify = t.get("verify")
    cmd = verify.get("cmd") if isinstance(verify, Mapping) else None
    if cmd:
        out += f" · verify: {one(cmd)}"
    if t.get("proves"):
        out += f" · proves {one(', '.join(_s(x) for x in t['proves']))}"
    return out


def _task_rows(out: dict[str, Any], value: Any, before: Any, note: str | None) -> None:
    """R3 (``task.edit``): what changed in the plan as ``before -> after`` rows, one per task that was added, changed or
    removed. ``before`` is the plan as the host read it; the signer has checked it against the event's ``base_rev``
    (``signer._before``), so ``note`` is ``None`` for a checked ``before``. A plan the plain view cannot compare shows
    only the whole new list (``tasks``), which is still complete."""
    if not isinstance(before, list) or not all(isinstance(t, Mapping) for t in before):
        return
    new = {_s(t.get("id")): t for t in value or [] if isinstance(t, Mapping)}
    old = {_s(t.get("id")): t for t in before}
    if len(new) != len(value or []) or len(old) != len(before):  # a repeated id: no comparison is claimed
        return
    for tid in [*old, *(i for i in new if i not in old)]:
        was, now = old.get(tid), new.get(tid)
        if was is not None and now is not None and dict(was) == dict(now):
            continue
        _row(
            out,
            f"Task {tid}",
            _task_words(now) if now is not None else "(removed)",
            _task_words(was) if was is not None else None,
            note,
        )


def policy_words(policy: Mapping[str, Any]) -> str:
    """A gate policy in plain words; every key of it is in the sentence."""
    _check_keys("policy keys", policy, _POLICY_SHOWN)
    approvers = ", ".join(_s(x) for x in policy.get("approvers") or []) or "nobody"
    n = policy.get("count")
    out = f"{n} approval{'s' if n != 1 else ''} from {approvers}"
    if policy.get("not"):
        out += f"; not by {', '.join(_s(x) for x in policy['not'])}"
    applies = policy.get("applies")
    out += "; applies to " + ("all tickets" if applies == "all" else "no ticket" if applies == "off" else _s(applies))
    out += "; approvers independent of the author" if policy.get("independent") else ""
    return out


def clean_tree(value: Any) -> Any:
    """Every string of ``value`` through :func:`orch.canon.clean` (the escaping of the terminal review): no control or
    invisible character reaches a browser either."""
    if isinstance(value, str):
        return canon.clean(value)
    if isinstance(value, list):
        return [clean_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_tree(v) for k, v in value.items()}
    return value


def headline(prepared: Mapping[str, Any], *, verified: bool) -> str:
    return build(prepared, verified=verified)["headline"]


def build(prepared: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    """The ``SignContent`` of a prepared request (see the module doc). Raises :class:`PlainUnavailable` for anything
    it does not know how to show.

    ``verified`` says whether the caller ran the signer's material checks (:func:`orch.host.signer.check` or
    :func:`orch.host.signer.verify_material`: the gate, policy, question and section hashes, every ``before`` against
    the event's ``base_rev``) on exactly this request. Without them nothing the host sent is vouched for: every
    ``before`` is labelled "per host, not checked" and the gate's policy row is left out."""
    material, events = prepared["material"], list(prepared["events"])
    if not events:
        raise PlainUnavailable("a request without events")
    if material.get("sequence") is not None:  # batch 11: the first request of a sequence is one review of all its steps
        from . import sequence

        try:
            return _with_cancels(_finish(sequence.build_content(prepared, verified=verified)), prepared)
        except sequence.SequenceInvalid as exc:
            raise PlainUnavailable(str(exc)) from None
    ticket = _ticket(material, events)
    out: dict[str, Any] = {"headline": ""}
    ctx = _Ctx(verified)
    if ticket is not None:
        out["ticket"] = ticket
    kind = material.get("kind")
    if kind == "gate":
        _gate(material, events, ticket, out, ctx)
    elif kind == "question":
        _question(material, events, ticket, out)
    elif kind == "payload":
        _payload(material, events, ticket, out, ctx, op=prepared.get("op"))
    else:
        raise PlainUnavailable(f"the plain view does not know the material kind {kind!r}")
    if prepared.get("op") in ("ac.accept", "ac.unaccept"):
        from . import acceptance

        statement = material.get("statement")
        parsed = acceptance.parse(statement)
        if parsed is None or len(events) != 1 or not acceptance.matches(events[0], statement):
            raise PlainUnavailable("manual acceptance has no checked statement")
        ac = one(parsed["ac"])
        out["headline"] = (
            f"Accept {ac} of {_ref(ticket)} manually"
            if parsed["kind"] == "accept"
            else f"Withdraw the acceptance of {ac} of {_ref(ticket)}"
        )
        out["sections"] = [{"id": "statement", "title": "Statement", "text": statement}]
        out["notes"] = (
            ["No automated check backs this: you vouch for it yourself."] if parsed["kind"] == "accept" else []
        )
    done = {k: v for k, v in out.items() if v not in (None, [], "")}
    creates = any(ev.get("type") == "ticket.created" for ev in events)
    if creates:
        done = _new_ticket(done)
    if isinstance(done.get("ticket"), dict):  # from the signed events, never from the headline's words
        done["ticket"] = {**done["ticket"], "new": creates}
    return _with_cancels(_finish(done), prepared)


def _cancels(value: Any) -> list[dict[str, Any]]:
    """``prepared["cancels"]`` (:mod:`orch.host.cancels`) checked to its exact shape; anything else is refused, so a
    host cannot slip text into the review through it."""
    from orch.model.types import GATES

    if not isinstance(value, list):
        raise PlainUnavailable("cancels is not a list")
    out = []
    seen: set[str] = set()
    for c in value:
        if not isinstance(c, Mapping) or set(c) != {"gate", "people", "complete"}:
            raise PlainUnavailable("a cancels entry has other keys than gate, people, complete")
        gate, people = c["gate"], c["people"]
        if gate not in GATES or gate in seen or type(c["complete"]) is not bool:
            raise PlainUnavailable("a cancels entry names an unknown or repeated gate")
        if not isinstance(people, list) or not people or len(people) > 64:
            raise PlainUnavailable("a cancels entry without people")
        if not all(isinstance(x, str) and _PERSON.fullmatch(x) for x in people) or len(set(people)) != len(people):
            raise PlainUnavailable("a cancels entry names a malformed person")
        seen.add(gate)
        out.append({"gate": gate, "people": list(people), "complete": c["complete"]})
    if [c["gate"] for c in out] != [g for g in GATES if g in seen]:
        raise PlainUnavailable("cancels are not in gate order")
    return out


_PERSON = re.compile(r"p_[0-9a-f]{32}")


def _with_cancels(content: dict[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    """R3: the host's word on which approvals exist now and would be cancelled, beside the signed (deterministic)
    note "This cancels any existing approval of ...". ``content["cancels"]`` is the structure (the dashboard shows the
    names); one note says it in words, always with "per host, not signed". A request without ``cancels`` is
    unchanged."""
    if "cancels" not in prepared or prepared["cancels"] is None:
        return content
    rows = _cancels(prepared["cancels"])
    notes = list(content.get("notes") or [])
    if rows:
        parts = [
            f"{g['gate']} ({len(g['people'])} {'person' if len(g['people']) == 1 else 'people'}"
            f"{'' if g['complete'] else ', not yet complete'})"
            for g in rows
        ]
        notes.append(
            "Per the host (not signed): approvals that stand right now and are cancelled by this: "
            + ", ".join(parts)
            + "."
        )
        content = {**content, "cancels": rows}
    elif any(isinstance(n, str) and n.startswith(_CANCEL_HEAD) for n in notes):
        notes.append("Per the host (not signed): no approval stands right now, so this cancels none.")
    if notes:
        content = {**content, "notes": notes}
    return content


def _new_ticket(content: dict[str, Any]) -> dict[str, Any]:
    """U3: this request creates the ticket (a signed ``ticket.created`` is among its events, and a gate approval can
    only follow its ticket's creation), so there is no earlier approval an edit could cancel: the two cancel notes are
    provably empty and go. Its rows have no earlier value, so they are the ticket's details, not "changed fields"."""
    notes = [n for n in content.get("notes") or [] if not (isinstance(n, str) and n.startswith(_NOTHING_TO_CANCEL))]
    if notes:
        content["notes"] = notes
    else:
        content.pop("notes", None)
    rows = content.get("fields_changed") or []
    if rows and not content.get("fields_title") and all(r.get("before") is None for r in rows):
        content["fields_title"] = "Details"
    return content


_NOTHING_TO_CANCEL = (
    "This cancels any existing approval of: ",
    "An add-on field or section can also be part of an approval;",
)


def _finish(content: dict[str, Any]) -> dict[str, Any]:
    return _with_title(_dedupe_notes(content))


_NEW_TICKET = (re.compile(r"Create ticket (\S+) · (.*)", re.S), re.compile(r"Create (\S+): (.*)", re.S))


def _with_title(content: dict[str, Any]) -> dict[str, Any]:
    """U3: ``title``, the headline a person reads: no "key per host; signed id" qualifier in the words and "New ticket
    KEY: title" for a creation. Only ever a rewording of ``headline``.

    ``ticket.new`` (set by :func:`build` from the request's own events: any ``ticket.created``; a sequence takes it from
    its expanded events) says whether this review creates the ticket (a creation signs the key and the title, so the
    signed id is only a technical detail there). For a request on an existing ticket the qualifier is not in the title
    but the dashboard keeps "key per host · signed id …tail" visible in the main view (``ticket.new`` false)."""
    head, ticket = content.get("headline"), content.get("ticket")
    if not isinstance(head, str):
        return content
    if isinstance(ticket, Mapping):
        head = head.replace(f" (key per host; signed id …{ticket.get('uid_tail')})", "")
    for pattern in _NEW_TICKET:
        m = pattern.fullmatch(head)
        if m:
            head = f"New ticket {m.group(1)}: {m.group(2)}"
            break
    if head != content["headline"]:
        content["title"] = head
    return content


_CANCEL_HEAD, _CANCEL_TAIL = "This cancels any existing approval of: ", ". Each one has to be approved again."


def _dedupe_notes(content: dict[str, Any]) -> dict[str, Any]:
    """Each note once: a review of several events (a sequence, a new ticket) would otherwise repeat the same sentence.
    Only exact repeats go. The cancel notes that differ only in their gate list become one note at the place of the
    first, naming the union of the gates in gate order; every other note keeps its text and its place."""
    notes = content.get("notes")
    if not notes:
        return content
    from orch.model.generations import GATES

    seen: set[str] = set()
    kept: list[str] = []
    cancel_at: int | None = None
    cancelled: set[str] = set()
    for note in notes:
        if isinstance(note, str) and note.startswith(_CANCEL_HEAD) and note.endswith(_CANCEL_TAIL):
            names = {g.strip() for g in note[len(_CANCEL_HEAD) : -len(_CANCEL_TAIL)].split(",")}
            if names <= set(GATES):
                cancelled |= names
                if cancel_at is None:
                    cancel_at = len(kept)
                    kept.append("")  # filled below
                continue
        if note not in seen:
            seen.add(note)
            kept.append(note)
    if cancel_at is not None:
        kept[cancel_at] = _CANCEL_HEAD + ", ".join(g for g in GATES if g in cancelled) + _CANCEL_TAIL
    content["notes"] = kept
    return content


class _Ctx:
    """What the caller vouched for (``verified``) and the label of a ``before`` nothing signed binds."""

    def __init__(self, verified: bool) -> None:
        self.verified = verified
        self.grants: Any = None  # material["grants"]: the host's word about each access taken back (not signed)
        self.taken = 0  # how many accesses the request takes back, and how many rows were made so far
        self.rows = 0

    def note(self, bound: bool) -> str | None:
        """The ``before_note`` of a ``before``: ``bound`` is "the event's ``base_rev`` binds it"."""
        if not self.verified:
            return "per host, not checked"
        return None if bound else "per host, not signed"


# --- gate decisions -------------------------------------------------------------------------------------------------


def _gate(
    material: Mapping[str, Any], events: list[Mapping[str, Any]], ticket: Any, out: dict[str, Any], ctx: _Ctx
) -> None:
    if len(events) != 1:
        raise PlainUnavailable("a gate request holds one decision")
    ev = events[0]
    typ = _s(ev.get("type"))
    if typ not in ("gate.approved", "gate.changes_requested", "verdict.given"):
        raise PlainUnavailable(f"a gate request cannot hold {typ}")
    _check_keys(typ, ev, SHOWN[typ], TECHNICAL_KEYS)
    gate_id = _s(material.get("gate"))
    if "gate" in ev and ev["gate"] != gate_id:
        raise PlainUnavailable("the event and the material name different gates")
    gate = _title(gate_id).lower()
    verb = {
        "gate.approved": f"Approve the {gate} of",
        "gate.changes_requested": f"Ask for changes to the {gate} of",
        "verdict.given": f"Give the verdict {_s(ev.get('outcome'))} on the {gate} of",
    }[typ]
    out["headline"] = f"{verb} {_ref(ticket)}"
    texts = sorted((material.get("texts") or {}).items())
    out["sections"] = [{"id": sid, "title": _title(sid), "text": body} for sid, body in texts if body]
    # N12: a bound section with no text is noise in the plain view, said in one line (the full review lists each)
    empty = [_title(sid) for sid, body in texts if not body]
    if empty:
        out.setdefault("notes", []).append(f"Empty, not shown: {', '.join(empty)}.")
    out["fields_title"] = "Applies under"  # initial values of the ticket and the rule, not changes (N12)
    if ev.get("text"):
        out["sections"].append({"id": "comment", "title": "Your comment", "text": _s(ev["text"])})
    inp = material.get("input")
    if not isinstance(inp, Mapping):
        raise PlainUnavailable("a gate request without its input")
    _check_keys("gate input keys", inp, _INPUT_SHOWN, _INPUT_TECHNICAL)
    fields = inp.get("fields") or {}
    _check_keys("gate fields", fields, _FIELDS_SHOWN)
    if fields.get("acceptance"):
        out["acceptance"] = _acceptance(fields["acceptance"])
    if inp.get("tasks"):
        out["tasks"] = _tasks(inp["tasks"])
    for name in ("ticket_type", "size", "links"):
        if fields.get(name) is not None:
            _row(out, "type" if name == "ticket_type" else name, fields[name])
    for addon, value in sorted((fields.get("addons") or {}).items()):
        _row(out, f"add-on {addon}", value)
    for name, a in sorted((inp.get("artifacts") or {}).items()):
        _check_keys("gate artifact keys", a, _ARTIFACT_SHOWN)
        extra = "".join(f"; {k} {_s(a[k])}" for k in ("ac", "task") if a.get(k))
        _row(out, f"file {name}", f"{_s(a.get('kind'))}, digest {_short(a.get('digest'))}{extra}")
    manual = material.get("manual_acceptances", [])
    if not isinstance(manual, list) or (manual and gate_id != "verify"):
        raise PlainUnavailable("manual acceptances require a verify gate list")
    for a in manual:
        if not isinstance(a, Mapping):
            raise PlainUnavailable("malformed manual acceptance")
        _check_keys("manual acceptance keys", a, frozenset({"ac", "name", "by"}))
        if set(a) != {"ac", "name", "by"} or not all(isinstance(v, str) for v in a.values()):
            raise PlainUnavailable("malformed manual acceptance")
    for a in sorted(manual, key=lambda a: (a["ac"], a["name"], a["by"])):
        _row(out, "accepted manually", f"{a['ac']} by {a['by']} (per host)")
    for tid, r in sorted((inp.get("receipts") or {}).items()):
        _check_keys("gate receipt keys", r, _RECEIPT_SHOWN)
        _row(
            out,
            f"result of {tid}",
            f"{_s(r.get('repo'))} commit {_short(r.get('commit'))}, exit {_s(r.get('exit'))}, "
            f"event {_s(r.get('event'))}",
        )
    for label, items in (("code", inp.get("source_sha")), ("code you decide on", ev.get("source_sha"))):
        for item in items or []:
            _check_keys("source_sha keys", item, frozenset({"repo", "ref", "sha"}))
            _row(out, label, f"{_s(item.get('repo'))} {_s(item.get('ref'))} at {_short(item.get('sha'))}")
    for earlier, p in sorted((inp.get("prior") or {}).items()):
        _check_keys("prior gate keys", p, frozenset({"gen", "approvals"}))
        who = ", ".join(_s(x) for x in p.get("approvals") or []) or "nobody"
        _row(out, f"earlier gate {earlier}", f"generation {_s(p.get('gen'))}, approved by {who}")
    policy = material.get("policy")
    if not isinstance(policy, Mapping):
        raise PlainUnavailable("a gate request without its policy")
    if ctx.verified:  # the policy is shown only when the signer recomputed its hash from it
        _row(out, "rule for this gate", policy_words(policy))


def _question(material: Mapping[str, Any], events: list[Mapping[str, Any]], ticket: Any, out: dict[str, Any]) -> None:
    if len(events) != 1:
        raise PlainUnavailable("a question request holds one answer")
    ev = events[0]
    _check_keys("question.answered", ev, SHOWN["question.answered"], TECHNICAL_KEYS)
    q = material.get("question") or {}
    _check_keys("question keys", q, frozenset({"id", "text", "why", "options", "recommended", "to", "blocking"}))
    for o in q.get("options") or []:
        _check_keys("question option keys", o, frozenset({"key", "label", "cost"}))
    keys = [_s(o.get("key")) for o in q.get("options") or []]
    if len(set(keys)) != len(keys):
        raise PlainUnavailable("a question with the same option key twice")
    opts = {_s(o.get("key")): _s(o.get("label")) for o in q.get("options") or []}
    picked = ev.get("option")
    answer = opts.get(_s(picked), _s(picked)) if picked else "your own words"
    out["headline"] = f"Answer “{_line1(q.get('text'), 60)}” on {_ref(ticket)} with {answer}"
    out["sections"] = [{"id": "question", "title": "Question", "text": _s(q.get("text", ""))}]
    if q.get("why"):  # the question hash binds the text and the options, not this: the host's word
        out["sections"].append({"id": "why", "title": "Why it was asked (per host, not signed)", "text": _s(q["why"])})
    if ev.get("text"):
        out["sections"].append({"id": "answer", "title": "Your answer", "text": _s(ev["text"])})
    if ev.get("question") != q.get("id"):
        raise PlainUnavailable("the event and the material name different questions")
    if picked:
        # The key too: two options may carry the same label, and the row is what the strip shows and the person signs.
        _row(
            out,
            "your choice",
            f"{answer} (option {_s(picked)})" if _s(picked) in opts else f"{answer} (not one of the options)",
        )
    for o in q.get("options") or []:
        _row(
            out,
            f"option {_s(o.get('key'))}",
            _s(o.get("label")) + (f" (cost {_s(o['cost'])})" if o.get("cost") else ""),
        )
    # who it is addressed to, whether it blocks and which option is recommended are the host's word: the question hash
    # binds none of them. Said in plain words, each row labelled "(per host)" and listed after the signed rows.
    out["fields_title"] = "About this question"  # facts of the question, not changes: no "- ->" before values
    for k, label in (("to", "addressed to"), ("blocking", "blocks the ticket"), ("recommended", "recommended option")):
        if k in q:
            value = q[k]
            if value == "ticket_owner":
                words = "the ticket owner"
            elif value is True or value is False:
                words = "yes" if value else "no"
            elif k == "recommended":
                words = opts.get(_s(value), _s(value))
            else:
                words = _s(value)
            _row(out, f"{label} (per host)", words)  # the host's word: the question hash binds none of these


# --- everything else ------------------------------------------------------------------------------------------------


def _payload(
    material: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    ticket: Any,
    out: dict[str, Any],
    ctx: _Ctx,
    op: Any = None,
) -> None:
    before = material.get("before") or {}
    texts = material.get("texts") or {}
    ref = _ref(ticket)
    heads: list[str] = []
    ctx.grants = material.get("grants")
    taken = [_grant_words(ev, ctx)[0] for ev in events if ev.get("type") == "grant.revoked"]
    ctx.taken = len(taken)
    if len(set(taken)) != len(taken):  # two accesses that read alike are never shown as one
        raise PlainUnavailable("two accesses taken back cannot be told apart")
    for ev in events:
        typ = _s(ev.get("type"))
        if typ not in SHOWN:
            raise PlainUnavailable(f"the plain view does not know the event type {typ}")
        _check_keys(typ, ev, SHOWN[typ], TECHNICAL_KEYS)
        heads.append(_event(typ, ev, ref, out, before, texts, ctx))
    issued = [ev.get("grant") for ev in events if ev.get("type") == "grant.issued"]
    revoked = [ev.get("grant") for ev in events if ev.get("type") == "grant.revoked"]
    if issued and revoked:  # an Extend: the access it replaces must be another one than the new access
        if set(issued) & set(revoked):
            raise PlainUnavailable("the access to be taken back is the one being given")
    shown = {}
    for gid in [*issued, *revoked]:
        if shown.setdefault(_grant_short(gid), gid) != gid:  # two different accesses that look alike on the screen
            raise PlainUnavailable("two accesses share one short form")
    if op == "publish.share" and len(events) == 1 and events[0].get("type") == "log.added":
        # exactly one share: a mix, or a second share, is never summarised (a summary of one would hide the other)
        summary = _share_summary(_s(events[0].get("text")))
        if summary is not None:
            out["summary"] = summary
    if len(taken) > 1:  # several take-backs: one headline with the count; each access has its own row
        heads = [h for h, ev in zip(heads, events, strict=True) if ev.get("type") != "grant.revoked"]
        heads.append(f"Take back {len(taken)} agent accesses")
    uniq = list(dict.fromkeys(heads))
    extend = _extend_summary(events, ctx) if issued and revoked else None
    if extend is not None:
        out["headline"] = extend.pop("headline")
        out["summary"] = extend
    elif len(uniq) == 1:
        out["headline"] = uniq[0]
    elif len(uniq) <= 3:
        out["headline"] = " and ".join(uniq)
    else:
        out["headline"] = f"{uniq[0]} (+{len(uniq) - 1} more changes)"


def _event(
    typ: str,
    ev: Mapping[str, Any],
    ref: str,
    out: dict[str, Any],
    before: Mapping[str, Any],
    texts: Mapping[str, str],
    ctx: _Ctx,
) -> str:
    """The headline of one event; everything it shows is added to ``out``."""
    if typ == "ticket.created":
        title = _s(ev.get("title"))
        out["ticket"] = {**(out.get("ticket") or {}), "key": _s(ev.get("key")), "title": one(title)}
        _row(out, "type", ev.get("ticket_type"))
        _row(out, "owner", ev.get("owner"))
        tail = (out.get("ticket") or {}).get("uid_tail")
        return f"Create ticket {_s(ev.get('key'))} · {one(title)}" + (
            f" (key per host; signed id …{tail})" if tail else ""
        )
    if typ == "ticket.updated":
        return _updated(ev, ref, out, before, texts, ctx)
    if typ == "log.added":
        from orch.publishclient.approval import MARK

        text = _s(ev.get("text"))
        if text.startswith(MARK + "\n"):  # P5: a share approval; the words are the whole of what is signed
            out.setdefault("sections", []).append({"id": "share", "title": "What will be shared", "text": text})
            out.setdefault("notes", []).append(
                "Files leave this computer only after you sign. You will see the link once; orch keeps no copy of it."
            )
            return f"Share files of {ref} outside this computer"
        out.setdefault("sections", []).append({"id": "note", "title": "Note", "text": text})
        return f"Add a note to {ref}"
    if typ in ("ticket.closed", "ticket.reopened"):
        if ev.get("text"):
            out.setdefault("sections", []).append({"id": "reason", "title": "Message", "text": _s(ev["text"])})
        if ev.get("duplicate_of"):
            _row(out, "duplicate of", ev["duplicate_of"])
        if typ == "ticket.reopened":
            return f"Reopen {ref}"
        return f"Close {ref} as {_s(ev.get('resolution'))}"
    if typ == "ticket.moved":
        _row(out, "from", ev.get("from"))
        if ev.get("reason"):
            _row(out, "reason", ev["reason"])
        return f"Move {ref} to {_s(ev.get('to'))}"
    if typ == "grant.issued":
        return _grant_issued(ev, out)
    if typ == "grant.revoked":
        # which access, in words by the host's word (labelled); the id is in the technical details only (DETAILS_ONLY)
        words, _ = _grant_words(ev, ctx)
        ctx.rows += 1
        _row(
            out,
            "access taken back" + (f" {ctx.rows}/{ctx.taken}" if ctx.taken > 1 else ""),
            words[0].upper() + words[1:] + " (per host, not signed)",
        )
        note = "Which access is the host's word, not signed; the access id is in the technical details."
        if note not in out.setdefault("notes", []):
            out["notes"].append(note)
        if ev.get("reason") == "extended":  # B8: the old half of an Extend; said ONCE (see _EXTEND_WARNING)
            if _EXTEND_WARNING not in out["notes"]:
                out["notes"].append(_EXTEND_WARNING)
        if ev.get("reason"):
            _row(out, "reason", ev["reason"])
        return "Take back agent access"  # signed words only: which access is in the labelled row (the host's word)
    if typ == "settings.changed":
        for key, value in sorted((ev.get("set") or {}).items()):
            if key == "repos" and _repo_rows(out, value, before.get(key), ctx):
                continue
            if key == "publish" and _publish_rows(out, value, before.get(key), ctx):
                continue
            if key == "models" and _models_rows(out, value, before.get(key), ctx):
                continue
            if key == "icon" and _icon_rows(out, value, before.get(key), ctx):
                continue
            _row(out, key, value, before.get(key), ctx.note(False))
        if "grant_hours" in (ev.get("set") or {}):  # B8: the longest agent access, in days/hours
            out.setdefault("notes", []).append(
                f"Agents may get access for up to {span(ev['set']['grant_hours'])} (members; the runner too)."
            )
        return "Change the workspace settings"
    if typ == "hub.joined":  # V3 C2a/C2b: the words say what the hub does and does not get
        hub = _short(ev.get("hub_id"))
        mode = ev.get("mode")
        if mode not in ("selective", "full"):
            raise PlainUnavailable("only the selective and full modes have plain words")
        _row(out, "hub", ev.get("hub_id"))
        _row(out, "hub key fingerprint", _fp(ev.get("hk_root_pub")))
        _row(out, "mode", mode)
        _row(out, "binding", ev.get("binding_seq"))
        aliases = ev.get("aliases") if isinstance(ev.get("aliases"), list) else []
        for a in aliases:
            if not isinstance(a, dict) or set(a) != {"person_id", "user_id"}:
                raise PlainUnavailable("hub.joined alias is not a pair")
            _row(out, "hub name for you", f"{one(a['person_id'])} is {one(a['user_id'])} on the hub")
        if mode == "full":
            out.setdefault("notes", []).append(
                "Full: the hub will be able to read everything in this workspace, restricted tickets too. It becomes "
                "a member of the workspace and gets the workspace key (sealed to the hub's own key). Leaving later "
                "starts a new key the hub never gets, but it cannot take back what the hub already read. Only you can"
                " sign this, with your own key at this terminal."
            )
            return f"Join hub {hub} in full mode: the hub will be able to read everything in this workspace"
        out.setdefault("notes", []).append(
            "Selective: the hub sees only what you choose to share. It never gets the workspace key and is not a "
            "member of the workspace. You can leave at any time (orch hub leave), also while the hub is down."
        )
        return f"Join hub {hub} in selective mode: the hub sees only what you share"
    if typ == "hub.left":
        _row(out, "hub", ev.get("hub_id"))
        _row(out, "binding", ev.get("binding_seq"))
        out.setdefault("notes", []).append(
            "The workspace tells the hub and publish that it is no longer bound. This does not take back what the "
            "hub already read. If the hub was in full mode, the workspace key is replaced by a new one that the hub "
            "never gets."
        )
        return f"Leave hub {_short(ev.get('hub_id'))}"
    if typ == "people.changed":
        role = _s(ev.get("role"))
        add, remove = ev.get("add") or [], ev.get("remove") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            raise PlainUnavailable("people.changed lists are not lists")
        for verb, people in (("add", add), ("remove", remove)):
            for i, who in enumerate(people, 1):  # one row per person: the dashboard labels a lone person id by name
                _row(out, f"{role}: {verb}" + (f" {i}/{len(people)}" if len(people) > 1 else ""), one(who))
        who = ", ".join(one(x) for x in add) or ", ".join(one(x) for x in remove)
        if role == "owner":
            return f"Make {who} the owner of {ref}"
        return f"{'Add' if add else 'Remove'} {who} as {role[:-1] if role.endswith('s') else role} of {ref}"
    if typ == "policy.changed":
        for gate, p in sorted((ev.get("gates") or {}).items()):
            _row(out, f"rule for {gate}", policy_words(p))
        return "Change the approval rules" + (f" of {ref}" if ref != "the workspace" else "")
    if typ == "member.added":
        _row(out, "person", ev.get("person"))
        _row(out, "name", ev.get("name"))
        _row(out, "role", ev.get("role"))
        _row(out, "key fingerprint", _fp(ev.get("pk_pub")))
        _cert_rows(out, "device certificate", ev.get("device_cert"))
        return f"Add {_s(ev.get('name'))} as {_s(ev.get('role'))}"
    if typ == "member.removed":
        _row(out, "person", ev.get("person"))
        return f"Remove member {_s(ev.get('person'))}"
    if typ == "role.changed":
        _row(out, "person", ev.get("person"))
        return f"Make {_s(ev.get('person'))} {_s(ev.get('role'))}"
    if typ == "task.done":
        for key in ("task", "receipt", "log", "text"):
            if key in ev:
                _row(out, key, ev[key])
        if "receipt" not in ev:
            out.setdefault("notes", []).append("No receipt: marking this task done proves no criterion.")
        return f"Mark task {_s(ev.get('task'))} of {ref} done"
    if typ in ("artifact.added", "artifact.replaced"):
        for k in ("kind", "label", "ref", "addon", "ac", "task", "replaces"):
            if ev.get(k) is not None:
                _row(out, k, ev[k])
        _row(out, "size", f"{_s(ev.get('bytes'))} bytes")
        _row(out, "digest", _short(ev.get("sha256")))
        out.setdefault("notes", []).append(
            "The file itself is not shown here; compare the digest with the file you meant (details: d)."
        )
        return f"{'Add' if typ == 'artifact.added' else 'Replace'} the file {_s(ev.get('name'))} on {ref}"
    if typ in ("addon.disabled", "addon.purged", "addon.granted"):
        return _addon(typ, ev, out)
    if typ == "device.added":
        _row(out, "device", ev.get("device"))
        _cert_rows(out, "certificate", ev.get("cert"))
        return "Add a paired device to your devices"
    if typ == "question.asked":
        out.setdefault("sections", []).append({"id": "question", "title": "Question", "text": _s(ev.get("question"))})
        _row(out, "question id", ev.get("qid"))
        return f"Ask a question on {ref}"
    raise PlainUnavailable(f"the plain view does not know the event type {typ}")


#: what each capability of the closed list (``orch.addons.manifest.CAPABILITIES``) lets an add-on do, in words
_CAPABILITY_WORDS = {
    "serve_http": "serve web pages from this computer",
    "spawn_agent": "start agents on this computer, as you",
    "pty": "open terminals on this computer, as you",
    "network": "use the internet",
    "git_push": "push your code to remote repositories",
}
_ADDON_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]{0,31}")


#: said in EVERY plain add-on review: the capabilities are declared, not enforced (add-ons are not sandboxed)
ADDON_TRUST = "An add-on runs its own code on this computer as you; turn it on only if you trust it."


def _capability_words(value: Any) -> str | None:
    """The capabilities of an ``addon.granted`` in words, or ``None`` when any is not on the closed list (or the value
    is not a list of distinct strings): the rows of the signed values are shown instead."""
    if not isinstance(value, list) or not all(isinstance(c, str) and c in _CAPABILITY_WORDS for c in value):
        return None
    if len(set(value)) != len(value):
        return None
    return "; ".join(_CAPABILITY_WORDS[c] for c in value) or ""


#: the built-in File Explorer reads project files inside the dashboard server, which no capability word names. Its grant
#: review says so in its own words (matched by name AND the digest of the built-in package, never by name alone)
EXPLORER_HEADLINE = "Turn on File Explorer: owners can read this workspace's files and git history in the dashboard"
EXPLORER_ROW = (
    "read every file of this workspace and its declared repositories, and their git history "
    "(read only; keys, .env files, .git and orch's own files stay locked)"
)


def _is_builtin_explorer(ev: Mapping[str, Any]) -> bool:
    from orch.addons import builtin
    from orch.addons.package import MANIFEST_FILE, package_digest

    try:
        return ev.get("name") == "explorer" and ev.get("package_sha256") == package_digest(
            {MANIFEST_FILE: builtin.manifest_bytes("explorer")}
        )
    except (ValueError, OSError):
        return False


def _addon(typ: str, ev: Mapping[str, Any], out: dict[str, Any]) -> str:
    """The add-on reviews. The plain view of an add-on that declares nothing but known capabilities (no fields,
    sections or artifact kinds) is its name, its version and what it may do in words; the package digest and the exact
    capability names are in the technical details (``d``). Anything else keeps every signed value as a row, so the plain
    words never claim less than is signed (WYSIWYS)."""
    name = _s(ev.get("name"))
    named = _ADDON_NAME.fullmatch(name) is not None
    nice = name.replace("-", " ").replace("_", " ").capitalize() if named else name
    if typ == "addon.disabled":
        return f"Turn off {nice}" if named else f"Disable the add-on {name}"
    if typ == "addon.purged":
        return f"Purge the add-on {name}"
    version = ev.get("version")
    words = _capability_words(ev.get("capabilities"))
    declares = any(ev.get(k) not in (None, [], {}) for k in ("fields", "sections", "artifact_kinds"))
    if named and words is not None and not declares and isinstance(version, str) and _VERSION.fullmatch(version):
        _row(out, "add-on", name)
        _row(out, "version", version)
        explorer = not words and _is_builtin_explorer(ev)
        if explorer:
            _row(out, "it lets owners", EXPLORER_ROW)
        else:
            _row(out, "it says it will", words or "(declares no capabilities)")
        notes = out.setdefault("notes", [])
        notes.append(ADDON_TRUST)
        notes.append("The package digest and the exact capability names are in the technical details.")
        if explorer:
            return EXPLORER_HEADLINE
        return f"Turn on {nice}: " + (f"it says it will {words}" if words else "it declares no capabilities")
    _row(out, "version", ev.get("version"))
    _row(out, "package digest", _short(ev.get("package_sha256")))
    for k in ("capabilities", "artifact_kinds", "fields", "sections"):
        if ev.get(k) is not None:
            _row(out, k.replace("_", " "), ev[k])
    return f"Grant the add-on {name}"


def _cert_rows(out: dict[str, Any], label: str, cert: Any) -> None:
    """A device certificate: the fingerprint of the whole certificate and the fields that decide what it may do."""
    _row(out, f"{label} fingerprint", _fp(cert))
    o = cert.get("o") if isinstance(cert, Mapping) else None
    if not isinstance(o, Mapping):
        raise PlainUnavailable("a certificate without its signed part")
    for k in ("person_id", "device_id", "scopes_max", "expires_ms"):
        if o.get(k) is not None:
            _row(out, f"{label} {k.replace('_', ' ')}", o[k])


#: the Estimate add-on's scale (the dashboard's ESTIMATE_SCALE): the signed value stays visible next to the points
_SIZE_POINTS = {"xs": 1, "s": 2, "m": 3, "l": 5, "xl": 8}


def _size_words(value: Any) -> Any:
    if isinstance(value, str) and value in _SIZE_POINTS:
        n = _SIZE_POINTS[value]
        return f"{n} point{'' if n == 1 else 's'} (size {value.upper()})"
    return value


def _cancel_note(ev: Mapping[str, Any], out: dict[str, Any]) -> None:
    """The gates an approval of which this edit cancels, from core's own binding list (``generations.BOUND_BASE``): a
    deterministic function of the signed change, so the host's word is not needed. Whether a gate is approved right
    now is not part of what is signed, so the words say "any existing approval"."""
    from orch.model.generations import BOUND_BASE, GATES

    paths = {p if p.startswith("ticket.") else "ticket." + p for p in (ev.get("set") or {})}
    paths |= {"body." + sid for sid in (ev.get("sections") or {})}
    hit = [g for g in GATES if paths & BOUND_BASE.get(g, set())]
    # ``generations.settle``: a raise of one gate raises every later gate
    gates = list(GATES[GATES.index(hit[0]) :]) if hit else []
    notes = out.setdefault("notes", [])
    if gates:
        notes.append(f"This cancels any existing approval of: {', '.join(gates)}. Each one has to be approved again.")
    if any(p.startswith("ticket.addons.") or ("." in p[len("body.") :] and p.startswith("body.")) for p in paths):
        notes.append(
            "An add-on field or section can also be part of an approval; "
            "if it is, this may also cancel that approval and every later one."
        )


def _updated(
    ev: Mapping[str, Any],
    ref: str,
    out: dict[str, Any],
    before: Mapping[str, Any],
    texts: Mapping[str, str],
    ctx: _Ctx,
) -> str:
    names: list[str] = []
    checked = set(ev.get("base_rev") or {})
    for sid, entry in sorted((ev.get("sections") or {}).items()):
        if isinstance(entry, Mapping):
            _check_keys("section keys", entry, frozenset({"hash", "refs"}))
            if entry.get("refs"):
                _row(out, f"{_title(sid)} references", list(entry["refs"]))
        out.setdefault("sections", []).append({"id": sid, "title": _title(sid), "text": texts.get(sid, "")})
        names.append(_title(sid).lower())
    for path, value in sorted((ev.get("set") or {}).items()):
        field = path[len("ticket.") :] if path.startswith("ticket.") else path
        if field == "acceptance":
            out["acceptance"] = _acceptance(value)
            names.append("acceptance criteria")
        elif field == "tasks":
            out["tasks"] = _tasks(value)
            _task_rows(out, value, before.get(path), ctx.note(path in checked))
            names.append("plan")
        elif field == "links" and _links_rows(out, value, before.get(path), ctx.note(path in checked)):
            names.append("repositories")
        elif field == "size":
            _row(out, "estimate", _size_words(value), _size_words(before.get(path)), ctx.note(path in checked))
            names.append("estimate")
        else:
            # `before` is shown as checked only when the event's base_rev binds it; otherwise it is the host's word
            _row(out, field, value, before.get(path), ctx.note(path in checked))
            names.append(field)
    _cancel_note(ev, out)
    return f"Change the {', '.join(names)} of {ref}" if names else f"Change {ref}"


# --- the terminal text ---------------------------------------------------------------------------------------------


def text(
    content: Mapping[str, Any],
    clean: Callable[[Any], str],
    *,
    workspace: str | None = None,
    members: list[Mapping[str, Any]] | None = None,
) -> list[str]:
    """``content`` as the terminal's plain view. Every value goes through ``clean`` (control characters never reach
    the terminal) and :func:`one` (a line break never starts a line of its own: structure such as ``2. task`` cannot be
    faked); only a section's text is a block, every line of it under a ``| `` mark.

    ``members`` (the workspace's members as ``{person, name, hidden?}``; ``None`` = not known, ids stay as signed) turns
    a person id in a row into name and short id by the dashboard's rules (:mod:`orch.host.personlabel`): the short id is
    always there, a name only when it passes those rules and then marked as the host's word (not signed); the full id is
    in the details of the review."""

    def v(value: Any) -> str:
        return clean(one(value))

    def who(value: Any) -> str:
        from . import personlabel

        said = None if members is None else personlabel.label(value, members, unsigned_name=True)
        return v(value) if said is None else clean(said)

    def block(value: Any) -> list[str]:
        lines = _s(value).split("\n") if value not in (None, "") else ["(empty)"]
        return ["  | " + clean(line) for line in lines]

    out = [v(content["headline"])]
    if workspace:
        out.append(f"in workspace {v(workspace)}")
    # N13: the host's word for the key ("per host, not proven") is in the technical details (`d`), not in this view
    out.append("")
    for sec in content.get("sections") or []:
        out += [v(sec["title"]), *block(sec["text"]), ""]
    if content.get("acceptance"):
        out.append("Acceptance criteria")
        out += [f"  [ ] {v(a['id'])}  {v(a['text'])}" for a in content["acceptance"]]
        out.append("")
    if content.get("tasks"):
        out.append("Plan")
        for t in content["tasks"]:
            ident = f" [{v(t['id'])}]" if t.get("id") else ""
            out.append(f"  {t['n']}.{ident} {v(t['title'])}")
            out += [f"       verify: {v(x)}" for x in t.get("verify") or []]
        out.append("")
    if content.get("fields_changed"):
        title = content.get("fields_title")
        out.append(
            title or ("Changes" if any(c["before"] is not None for c in content["fields_changed"]) else "Fields")
        )
        for c in content["fields_changed"]:
            new = "(not set)" if c["after"] is None else who(c["after"])
            if c["before"] is None:
                out.append(f"  {v(c['field'])}: {new}")
            else:
                note = f" ({v(c['before_note'])})" if c.get("before_note") else ""
                out.append(f"  {v(c['field'])}: {who(c['before'])}{note} -> {new}")
        out.append("")
    if content.get("cancels"):
        out.append("Approvals this cancels (per host, not signed)")
        for c in content["cancels"]:
            out.append(f"  {v(c['gate'])}: " + ", ".join(who(x) for x in c["people"]))
        out.append("")
    for note in content.get("notes") or []:
        out += [v(note), ""]
    return out
