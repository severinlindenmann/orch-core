"""Contract v1.3.1 read ops over the P1 model: ticket.query, ticket.get, needs, diff, artifact.get.

Reads only. Every function gets the viewer's already-visible ticket views, so a restricted ticket the person may not see
can never reach a row, a count, a page token, a log or a diff. Provisional wrappers: delete as stream A's host serves
these ops.
"""

import base64
import hashlib
import re
import secrets
import time

from orch.ops import views as p1_views
from orch.schema import SECTIONS_BY_TYPE
from orch.store import observe
from orch.store.render import HEADINGS

from orch_dashboard.errors import fail

STATUSES = ("backlog", "open", "in_progress", "testing", "done", "closed")
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
PAGE_TTL_S = 600
MAX_SNAPSHOTS = 32
SOURCE_ARGS = {"status", "type", "label", "assignee", "owner", "parent", "epic", "q", "updated_since", "needs_me"}
QUERY_ARGS = SOURCE_ARGS | {"sort", "order", "limit", "page"}
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
PERSON = re.compile(r"^p_[A-Za-z0-9]{1,64}$")
TAKES_ARGS = frozenset({"ticket.query", "ticket.get", "needs", "diff", "artifact.get"})


def bad(message="invalid input"):
    return fail("invalid.input", message, exit=5)


def _list_of(value, name, *, maximum, item=str, allowed=None):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum or not all(type(x) is item for x in value):
        raise bad(name)
    if allowed is not None and not all(x in allowed for x in value):
        raise bad(name)
    return value


def validate_query(args):
    if set(args) - QUERY_ARGS:
        raise bad("unknown argument")
    out = dict(args)
    if "status" in out:
        _list_of(out["status"], "status", maximum=8, allowed=STATUSES)
    for name in ("type", "label"):
        if name in out:
            _list_of(out[name], name, maximum=16)
            if not all(1 <= len(x) <= 64 for x in out[name]):
                raise bad(name)
    for name in ("assignee", "owner"):
        if name in out and (not isinstance(out[name], str) or not PERSON.fullmatch(out[name])):
            raise bad(name)
    for name in ("parent", "epic"):
        if name in out and (not isinstance(out[name], str) or not KEY.fullmatch(out[name])):
            raise bad(name)
    if "q" in out and (not isinstance(out["q"], str) or not 1 <= len(out["q"]) <= 200):
        raise bad("q")
    if "updated_since" in out and (
        not isinstance(out["updated_since"], str)
        or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", out["updated_since"])
    ):
        raise bad("updated_since")
    if "needs_me" in out and type(out["needs_me"]) is not bool:
        raise bad("needs_me")
    if out.get("sort", "updated") not in ("updated", "priority", "key"):
        raise bad("sort")
    if out.get("order", "desc") not in ("asc", "desc"):
        raise bad("order")
    if "limit" in out and (type(out["limit"]) is not int or not 1 <= out["limit"] <= 200):
        raise bad("limit")
    if "page" in out and (not isinstance(out["page"], str) or not 1 <= len(out["page"]) <= 512):
        raise bad("page")
    return out


def summary_row(store, v, person, visible):
    events = store.events(v.uid)
    updated = events[-1]["at"] if events else None
    epic = None
    parent = v.fields.get("parent")
    if parent:
        other = visible.get(parent)  # an invisible parent reveals nothing, not even its type
        epic = parent if other is not None and other.type == "epic" else None
    return {
        "key": v.key,
        "uid": v.uid,
        "title": v.title,
        "type": v.type,
        "status": v.status,
        "priority": v.fields["priority"],
        "size": v.fields["size"],
        "labels": list(v.fields["labels"]),
        "assignees": list(v.people["assignees"]),
        "owner": v.owner,
        "parent": parent,
        "epic": epic,
        "claim": {"session": v.claim.session, "agent": None} if v.claim else None,
        "gates": {g: "n/a" if not x.applies else "approved" if x.approved else "open" for g, x in v.gates.items()},
        "needs": [n.kind for n in v.needs if person in n.who],
        "updated_at": updated,
        "head_seq": store.head_seq(v.uid),
    }


def _key_number(key):
    prefix, _, number = key.rpartition("-")
    return prefix, int(number)


def _matches(store, row, view, args, person, texts):
    if "status" in args and row["status"] not in args["status"]:
        return False
    if "type" in args and row["type"] not in args["type"]:
        return False
    if "label" in args and not set(args["label"]) & set(row["labels"]):
        return False
    if "assignee" in args and args["assignee"] not in row["assignees"]:
        return False
    if "owner" in args and args["owner"] != row["owner"]:
        return False
    if "parent" in args and args["parent"] != row["parent"]:
        return False
    if "epic" in args and args["epic"] != row["epic"]:
        return False
    if "updated_since" in args and (row["updated_at"] or "") < args["updated_since"]:
        return False
    if args.get("needs_me") and not any(person in n.who for n in view.needs):
        return False
    if "q" in args:
        needle = args["q"].casefold()
        haystack = [row["key"], row["title"], *texts(view.uid).values()]
        if not any(needle in x.casefold() for x in haystack):
            return False
    return True


class Pages:
    """Snapshots behind page tokens: the first page fixes the rows, later pages only move the position."""

    def __init__(self):
        self.snapshots = {}

    def open(self, rows, as_of, sub_cursor, args_key):
        now = time.monotonic()
        self.snapshots = {k: v for k, v in self.snapshots.items() if v["expires"] > now}
        if len(self.snapshots) >= MAX_SNAPSHOTS:
            self.snapshots.pop(min(self.snapshots, key=lambda k: self.snapshots[k]["expires"]))
        sid = secrets.token_urlsafe(12)
        self.snapshots[sid] = {
            "rows": rows,
            "as_of": as_of,
            "cursor": sub_cursor,
            "key": args_key,
            "expires": now + PAGE_TTL_S,
        }
        return sid

    def read(self, token, args_key):
        sid, _, offset = token.partition(".")
        snap = self.snapshots.get(sid)
        if snap is None or snap["expires"] <= time.monotonic() or not offset.isdecimal():
            raise fail("page.expired", exit=9)
        if snap["key"] != args_key:
            raise bad("page")
        return sid, snap, int(offset)


def query(host, store, views, args, person):
    args = validate_query(args)
    limit = args.get("limit", 50)
    source = {k: v for k, v in args.items() if k in SOURCE_ARGS}
    sort, order = args.get("sort", "updated"), args.get("order", "desc")
    args_key = repr(sorted((k, repr(v)) for k, v in args.items() if k not in ("page", "limit")))
    if "page" in args:
        sid, snap, start = host.pages.read(args["page"], args_key)
        rows = snap["rows"]
    else:
        texts = store.body_sections
        visible = {v.key: v for v in views}
        for ref_name in ("parent", "epic"):
            if ref_name in source and source[ref_name] not in visible:
                raise fail("not_found", exit=2)
        rows = []
        for v in views:
            row = summary_row(store, v, person, visible)
            if _matches(store, row, v, source, person, texts):
                rows.append(row)
        rows.sort(key=lambda r: _key_number(r["key"]))  # ties break by key ascending
        if sort == "priority":
            rows.sort(key=lambda r: PRIORITY_RANK.get(r["priority"], 9), reverse=order == "asc")
        elif sort == "updated":
            rows.sort(key=lambda r: r["updated_at"] or "", reverse=order == "desc")
        else:
            rows.sort(key=lambda r: _key_number(r["key"]), reverse=order == "desc")
        # Python's sort is stable but reverse=True keeps ties in the original order, so ties stay key ascending.
        sid = host.pages.open(rows, store.state.now, host.cursor, args_key)
        snap = host.pages.snapshots[sid]
        start = 0
    end = start + limit
    return {
        "rows": rows[start:end],
        "next_page": f"{sid}.{end}" if end < len(rows) else None,
        "total": len(rows),
        "sub_cursor": snap["cursor"],
        "as_of": snap["as_of"],
    }


def needs(host, views, ws, person, args):
    if set(args) - {"person"}:
        raise bad("unknown argument")
    if "person" in args and args["person"] != person:
        raise fail("op.not_allowed", "another person's needs", exit=3)
    items = [
        {"kind": n.kind, "key": v.key, "uid": v.uid, "ref": n.ref, "detail": n.detail, "who": list(n.who)}
        for v in views
        for n in v.needs
        if person in n.who
    ]
    items += [
        {"kind": n.kind, "key": None, "uid": None, "ref": n.ref, "detail": n.detail, "who": list(n.who)}
        for n in ws.needs
        if person in n.who
    ]
    return {"items": items, "sub_cursor": host.cursor}


def _text(value, limit=200):
    """Host text, escaped: control and format characters become visible escapes, then cut."""
    out = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in str(value))
    return out[:limit]


def _find(views, ref):
    if not isinstance(ref, str) or not 1 <= len(ref) <= 64:
        raise bad("ref")
    wanted = ref.upper() if KEY.fullmatch(ref) else ref
    for v in views:
        if v.key == wanted or v.uid == ref:
            return v
    raise fail("not_found", exit=2)  # a ticket the viewer cannot see is the same as one that does not exist


def get(host, store, views, args, person):
    if set(args) - {"ref", "log_since", "log_limit"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    since, limit = args.get("log_since", 0), args.get("log_limit", 100)
    if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise bad("log")
    texts = store.body_sections(v.uid)
    sections = tuple(h for h in HEADINGS if h in SECTIONS_BY_TYPE[v.type])
    defs = {q["id"]: q for q in v.fields["questions"]}
    doc = {
        "key": v.key,
        "title": v.title,
        "type": v.type,
        "status": v.status,
        "priority": v.fields["priority"],
        "size": v.fields["size"],
        "labels": list(v.fields["labels"]),
        "due": v.fields["due"],
        "parent": v.fields["parent"],
        "blocked_by": list(v.fields["blocked_by"]),
        "owner": v.owner,
        "claim": {"session": v.claim.session, "live": v.claim.live} if v.claim else None,
        "waiting": v.waiting,
        "acceptance": [{"id": a.id, "text": a.text, "evidence": list(a.evidence)} for a in v.acceptance],
        "tasks": [{"id": t.id, "text": t.text, "state": t.state, "proves": list(t.proves)} for t in v.tasks],
        "questions": [
            {
                "id": q.id,
                "to": q.to,
                "blocking": q.blocking,
                "answered": q.answered,
                "text": defs.get(q.id, {}).get("text", ""),
                **({"options": [o["key"] for o in defs[q.id]["options"]]} if defs.get(q.id, {}).get("options") else {}),
            }
            for q in v.questions
        ],
        "current_state": texts.get("current_state", ""),
        "sections": {s: texts.get(s, "") for s in sections},
    }
    events = store.events(v.uid, after=since, limit=limit + 1)
    more = len(events) > limit
    events = events[:limit]
    log = []
    tail = p1_views.event_lines(events, since, {"acceptance": {}, "tasks": {}})
    for e, line in zip(events, tail, strict=False):
        actor = e["actor"]
        log.append(
            {
                "seq": e["seq"],
                "type": e["type"],
                "at": e["at"],
                "actor": {"kind": actor["kind"], "id": actor.get("id")},
                "summary": _text(line.partition(": ")[2]),
            }
        )
    questions = []
    for q in v.questions:
        answer = q.answer or {}
        questions.append(
            {
                "id": q.id,
                "state": "answered" if q.answered else "open",
                "to": list(q.addressees),
                "blocking": q.blocking,
                "answered_by": answer.get("by") if q.answered else None,
                "answered_seq": answer.get("seq") if q.answered else None,
            }
        )
    by_name = {a.name: a for a in v.artifacts}
    criteria = []
    for a in v.acceptance:
        evidence = []
        for ref in a.evidence:
            kind, _, name = ref.partition(":")
            art = by_name.get(name) if kind == "artifact" else None
            if art is not None and art.digest:
                evidence.append(
                    {
                        "artifact": art.name,
                        "digest": art.digest,
                        "kind": art.kind,
                        "attested_by": "person" if art.by.startswith("p_") else "agent",
                    }
                )
        criteria.append({"id": a.id, "text": a.text, "evidence": evidence})
    gates = {
        g: {
            "state": "n/a" if not x.applies else "approved" if x.approved else "open",
            "gen": x.gen,
            "hash12": p1_views.short_gate_hash(x.hash) if x.hash else None,
        }
        for g, x in v.gates.items()
    }
    return {
        "doc": doc,
        "log": log,
        "log_more": more,
        "questions": questions,
        "criteria": criteria,
        "gates": gates,
        "sub_cursor": host.cursor,
    }


def _unified_files(text):
    """Counts per file from a git numstat; rename and binary handling as git prints it."""
    files = []
    for line in text.splitlines():
        add, _, rest = line.partition("\t")
        delete, _, path = rest.partition("\t")
        binary = add == "-"
        files.append(
            {
                "path": path,
                "old_path": None,
                "status": "M",
                "add": 0 if binary else int(add),
                "del": 0 if binary else int(delete),
                "binary": binary,
            }
        )
    return files


def diff(store, views, args):
    if set(args) - {"ref", "repo", "context", "max_bytes"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    context, limit = args.get("context", 3), args.get("max_bytes", 262144)
    if type(context) is not int or not 0 <= context <= 10 or type(limit) is not int or not 1024 <= limit <= 1048576:
        raise bad("diff")
    links = v.fields["links"]
    wanted = list(links["repos"])
    if "repo" in args:
        if args["repo"] not in wanted:  # only a repo the ticket links
            raise bad("repo")
        wanted = [args["repo"]]
    sources, files, parts, truncated = [], [], [], False
    for name in wanted:
        branch = links["branches"].get(name)
        path = observe.repo_path(store.root, store.state.workspace.repos, name)
        if not branch or path is None or not path.is_dir():
            raise fail("observe.unavailable", f"{name}: no working copy or branch", exit=3)
        identity = observe.repo_identity(path, name)
        recorded = next((e for e in v.source_list if e["repo"] == identity), None)
        if recorded is None:
            raise fail("observe.unavailable", f"{name}: the host has not observed a branch head", exit=3)
        head = observe.git(path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}")
        # Bound to D58: the head shown is the source_sha a verdict would sign. A branch that moved since the host
        # observed it is refused, never shown as if it were that commit.
        if head != recorded["sha"]:
            raise fail("observe.unavailable", f"{name}: the branch moved since it was observed", exit=3)
        default = next(
            (b for b in ("main", "master") if observe.git(path, "rev-parse", "--verify", "-q", f"refs/heads/{b}")), None
        )
        base = observe.git(path, "merge-base", head, default) if default else None
        base = base or observe.git(path, "rev-parse", "--verify", "-q", f"{head}^")
        if not base:
            raise fail("observe.unavailable", f"{name}: no base to compare with", exit=3)
        sources.append({"repo": name, "branch": branch, "base_sha": base, "head_sha": head})
        numstat = observe.git(path, "diff", "--numstat", "-M50%", base, head) or ""
        names = observe.git(path, "diff", "--name-status", "-M50%", base, head) or ""
        status = {}
        for line in names.splitlines():
            code, *paths = line.split("\t")
            status[paths[-1]] = (code[:1] if code[:1] in "AMDRT" else "M", paths[0] if code[:1] == "R" else None)
        for f in _unified_files(numstat):
            real = f["path"]
            if " => " in real:  # git prints a rename as `old => new` (possibly with braces)
                real = next((p for p in status if real.endswith(p)), real)
            code, old = status.get(real, ("M", None))
            files.append({**f, "path": real, "repo": name, "status": code, "old_path": old})
        text = observe.git(path, "diff", f"-U{context}", "-M50%", base, head, strip=False) or ""
        parts.append(text)
    unified = "".join(parts)
    if len(unified.encode()) > limit:
        cut = unified.encode()[:limit].decode(errors="ignore")
        unified = cut[: cut.rfind("\n") + 1] if "\n" in cut else ""
        truncated = True
    return {"sources": sources, "files": files, "unified": unified, "truncated": truncated}


def artifact(store, views, args):
    if set(args) - {"ref", "name", "offset", "length"}:
        raise bad("unknown argument")
    v = _find(views, args.get("ref"))
    name, offset, length = args.get("name"), args.get("offset"), args.get("length", 786432)
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
        or type(offset) is not int
        or offset < 0
        or type(length) is not int
        or not 1 <= length <= 786432
    ):
        raise bad("artifact")
    art = next((a for a in v.artifacts if a.name == name and a.digest), None)
    if art is None:
        raise fail("not_found", exit=2)
    path = store.root / "tickets" / v.uid / "artifacts" / name  # name is validated: never joined from client text
    try:
        data = path.read_bytes()
    except OSError:
        raise fail("not_found", exit=2) from None
    if "sha256:" + hashlib.sha256(data).hexdigest() != art.digest and hashlib.sha256(data).hexdigest() != art.digest:
        raise fail("artifact.mismatch", exit=3)
    if offset > len(data):
        raise bad("offset")
    chunk = data[offset : offset + length]
    return {
        "b64": base64.b64encode(chunk).decode(),
        "size": len(data),
        "digest": art.digest,
        "offset": offset,
        "last": offset + len(chunk) >= len(data),
    }
