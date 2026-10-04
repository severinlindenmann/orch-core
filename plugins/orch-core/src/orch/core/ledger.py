"""The approval ledger: a per-user, append-only record of the human's approvals, answers, verdicts and closes, kept
outside the workspace and signed.

Ticket frontmatter and `orchestrator/.state/events.jsonl` live in the repository, where an agent works. The ledger
is one file per user, `ledger.jsonl` in the orch config dir (`$ORCH_STATE_DIR`, else `$XDG_CONFIG_HOME/orch`, else
`~/.config/orch`). Every line names its workspace (`workspace_id`: derived from the config's customer and id prefix,
so it survives worktrees, moves and renames) and carries an HMAC-SHA256 over its content with a per-user key in
`ledger.key` (mode 0600) next to it. Only human actions write it (`Ops.approve`, `Ops.answer`, `Ops.verdict`,
`Ops.close`, `Ops.ledger_adopt`, `Ops.epic_pause`, `Ops.set_widgets_html`; an epic approval also signs its charter), and a human action is refused under an agent harness (`lifecycle.require_human`);
the guard keeps agents' tools away from both files. One exception: turning a workspace setting off
(`Ops.set_widgets_html(False)`) takes power away, so any actor's off is signed too (it must outrank an earlier on).

Where an agent proceeds on a human decision (claim, task start/done, move to testing) the decision must be in the
ledger: an approval or a blocking answer without a signed entry, including one recorded before the ledger existed,
stops the agent until the human reviews it with `orch ledger adopt`. A child of an epic is backed by the epic's signed
charter, or by an agent's auto-approval under a signed, active delegation (orch.core.epics). Nothing in the repository (frontmatter stamps,
hash versions, the event log) can make a decision count as signed.

Limit: this is not OS isolation. Code running as the same user, outside the guard's view, can read the key and the
files like any other file of that user. The ledger makes forging a decision a deliberate act against files outside
the repository, not an edit of the ticket, and `orch check` reports what does not match.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from orch.clock import stamp_s
from orch.core.canonical import canonical_json
from orch.errors import OrchError

LEDGER_FILE = "ledger.jsonl"
KEY_NAME = "ledger.key"
_cache: dict[str, tuple[tuple, list[dict]]] = {}

NOT_SIGNED_HINT = ("ask the human to review it with `orch ledger adopt`; if they did not approve it, they should "
                   "request changes")


def base_dir() -> Path:
    from orch.dashboard.launch import config_dir
    return config_dir()


def workspace_id(ws) -> str:
    """sha256("<customer>|<prefix>")[:16], exactly as written in the config. A blank customer is allowed (`orch
    doctor` warns): an entry must still match the ticket and its gate or question hash."""
    cfg = ws.config
    ident = f"{cfg.get('customer', '')}|{(cfg.get('id') or {}).get('prefix', '')}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


WIDGETS_HTML = "widgets.html"
_checkouts: dict[str, str] = {}


def checkout_id(ws) -> str:
    """sha256 of the real path of the checkout's git common dir (one value for every worktree of a clone), else of
    the workspace root, [:16]. Setting entries carry it, so a decision made here does not carry over to a copy, a
    clone, a moved checkout or another workspace with the same customer and prefix."""
    key = str(ws.home)
    if key not in _checkouts:
        root = Path(ws.root).resolve()
        common = root
        for d in (root, *root.parents):
            git = d / ".git"
            if git.is_dir():
                common = git
                break
            if git.is_file():
                try:
                    gitdir = (d / git.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()).resolve()
                except (OSError, IndexError, ValueError):
                    break
                try:
                    common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
                except OSError:
                    common = gitdir
                break
        _checkouts[key] = hashlib.sha256(os.fsencode(common.resolve())).hexdigest()[:16]
    return _checkouts[key]


def _settings(ws, name: str, signed: list[dict]) -> list[dict]:
    cid = checkout_id(ws)
    return [e for e in signed if e.get("kind") == "setting" and e.get("setting") == name and e.get("checkout") == cid]


def record_setting(ws, name: str, value, actor, evidence: dict | None) -> dict:
    """Sign a workspace setting, chained: the entry's MAC covers `prev`, the MAC of this checkout's previous entry for
    the setting. The caller holds the workspace's config lock."""
    chain = _settings(ws, name, entries(ws))
    return record(ws, ticket=None, kind="setting", actor=actor, evidence=evidence, setting=name, value=value,
                  checkout=checkout_id(ws), prev=chain[-1]["mac"] if chain else "")


def signed_setting(ws, name: str, signed: list[dict] | None = None):
    """The value of this checkout's newest signed entry for `name`, or None when there is none or it does not chain
    onto the entry before it (a replayed, reordered or out-of-place entry): that counts as no decision."""
    chain = _settings(ws, name, entries(ws) if signed is None else signed)
    if not chain:
        return None
    last, prev = chain[-1], (chain[-2]["mac"] if len(chain) > 1 else "")
    if last.get("prev") != prev or sum(e["mac"] == last["mac"] for e in chain) > 1:
        return None
    return last.get("value")


def widgets_html_state(ws, signed: list[dict] | None = None) -> str:
    """Whether agent HTML runs in ticket widgets. "on": the config asks for it, this checkout's newest signed
    `widgets.html` entry says on, and the event log records no later off. "unsigned": the config asks for it without
    that (a hand edit, another machine or checkout, a broken chain, or a workspace from before the setting was
    signed): treated as off. "stale": the config says off but the newest signed entry still says on (an off made by
    hand, not through orch): off, but a later config edit would count. Else "off"."""
    cfg = (getattr(ws, "config", None) or {}).get("widgets") or {}
    asked = isinstance(cfg, dict) and cfg.get("html") is True
    if getattr(ws, "home", None) is None:
        return "off"
    if signed_setting(ws, WIDGETS_HTML, signed) is not True:
        return "unsigned" if asked else "off"
    if not asked:
        return "stale"
    from orch.core.events import read_events
    for ev in reversed(read_events(ws)):  # every orch change of the setting ends with this event
        if ev.kind == "setting.changed" and (ev.data or {}).get("setting") == WIDGETS_HTML:
            return "on" if ev.data.get("value") is True else "unsigned"
    return "on"


def ledger_path(ws=None) -> Path:
    return base_dir() / LEDGER_FILE


def key_path() -> Path:
    return base_dir() / KEY_NAME


_KEY_BYTES = 32


def _key(create: bool) -> bytes | None:
    """The signing key; None when there is none yet (and `create` is false). A key file shorter than 32 bytes is
    damaged: signing raises, reading treats every entry as unsigned."""
    path = key_path()
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = None
        if not create:
            return None
    if key is not None:
        if len(key) < _KEY_BYTES:
            if create:
                raise OrchError(f"the approval ledger key {path} is damaged (shorter than {_KEY_BYTES} bytes); "
                                "nothing was applied", hint="restore it from a backup, or remove it and the ledger "
                                "to start a new one (every decision then needs `orch ledger adopt`)")
            return None
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _key(create=True)  # another process created it first
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(_KEY_BYTES))
    return path.read_bytes()


def _mac(key: bytes, entry: dict) -> str:
    return hmac.new(key, canonical_json({k: v for k, v in entry.items() if k != "mac"}), hashlib.sha256).hexdigest()


def record(ws, *, ticket: str, kind: str, actor, evidence: dict | None, **fields) -> dict:
    """Append one signed entry (kind "gate", "answer", "verdict", "close", for epics "charter" and "pause",
    "ticket_request" for a backlog ticket a paired phone created, and "setting" for a workspace setting only the human
    turns on: ticket None, `setting` and `value`).
    Raises OrchError when it cannot be
    written, so the action it records is not applied without it."""
    entry = {"workspace": workspace_id(ws), "ticket": ticket, "kind": kind, **fields, "actor": actor.to_str(),
             "via": actor.via, "at": stamp_s(), "evidence": evidence}
    if getattr(actor, "device", None):
        entry["device"] = actor.device  # a phone decision (via "phone:<label>"): which paired phone
    try:
        key = _key(create=True)
        entry["mac"] = _mac(key, entry)
        path = ledger_path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        raise OrchError(f"could not write the approval ledger ({e}); nothing was applied",
                        hint=f"check that {base_dir()} is writable") from e
    return entry


def _stat(path: Path):
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def _read(path: Path, key: bytes, keysig) -> list[dict]:
    sig = (_stat(path), keysig)
    cached = _cache.get(str(path))
    if cached and cached[0] == sig:
        return cached[1]
    out: list[dict] = []
    if sig[0] is not None:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and isinstance(e.get("mac"), str) and hmac.compare_digest(e["mac"], _mac(key, e)):
                out.append(e)
    _cache[str(path)] = (sig, out)
    return out


def entries(ws) -> list[dict]:
    """This workspace's entries whose signature checks out (others are ignored), oldest first. Cached by file and
    key size and mtime, so a request may call it often."""
    key = _key(create=False)
    if not key:
        return []
    keysig = _stat(key_path())
    wid = workspace_id(ws)
    return [e for e in _read(ledger_path(ws), key, keysig) if e.get("workspace") == wid]


# A phone event and the kind of signed entry that must back it (orch.remote.verify signs through Ops as the phone's
# actor). A change request is not a signed decision: the remote ledger alone records it.
_PHONE_SIGNED = {"gate.approved": "gate", "question.answered": "answer", "verdict.given": "verdict",
                 "ticket.created": "ticket_request"}


def phone_event_signed(event, signed: list[dict]) -> bool | None:
    """Whether a signed entry backs a `phone:` event: same ticket, kind, `via` and a device, and the same gate (and
    hash), question or verdict. None for a kind that is never signed (a change request). The remote ledger beside
    the events is agent-writable; this one is not."""
    kind = _PHONE_SIGNED.get(event.kind)
    if kind is None:
        return None
    d = event.data or {}
    for e in signed:
        if (e.get("kind") != kind or e.get("ticket") != event.ticket or e.get("via") != event.via
                or not e.get("device")):
            continue
        if kind == "gate" and (e.get("gate") != d.get("gate") or (d.get("hash") and e.get("hash") != d.get("hash"))):
            continue
        if kind == "answer" and e.get("qid") != d.get("qid"):
            continue
        if kind == "verdict" and d.get("verdict") and e.get("verdict") != d.get("verdict"):
            continue
        return True
    return False


def gate_verification(ws, ticket, gate: str, signed: list[dict] | None = None, events=None, scan=None) -> str:
    """"none" (not approved), "verified" (a signed entry matches the stored hash: the gate's own approval, or the
    signed charter of the ticket's epic), "delegated" (an agent's approval under a signed, active delegation of
    its epic: see orch.core.epics), or "unverified"."""
    g = ((ticket.meta.get("gates") or {}).get(gate) or {})
    if not g.get("approved"):
        return "none"
    signed = entries(ws) if signed is None else signed
    for e in signed:
        if (e.get("kind") == "gate" and e.get("ticket") == ticket.id and e.get("gate") == gate
                and e.get("hash") == g.get("hash")):
            return "verified"
    if ticket.meta.get("parent"):
        from orch.core.epics import gate_coverage
        covered = gate_coverage(ws, ticket, gate, signed, events, scan)  # `scan`: a store.scan to reuse
        if covered:
            return covered
    return "unverified"


def answer_verification(ws, ticket, q: dict, signed: list[dict] | None = None) -> str:
    from orch.core.questions import question_hash
    if q.get("answer") in (None, ""):
        return "none"
    qh = question_hash(q)
    for e in entries(ws) if signed is None else signed:
        if (e.get("kind") == "answer" and e.get("ticket") == ticket.id and e.get("qid") == q.get("id")
                and e.get("answer") == q.get("answer") and e.get("question_hash") == qh):
            return "verified"
    return "unverified"


def done_verification(ws, ticket, *, closed: bool, signed: list[dict] | None = None) -> str:
    """The same for a done ticket: its human done verdict, or its human close."""
    signed = entries(ws) if signed is None else signed
    if closed:
        return "verified" if any(e.get("kind") == "close" and e.get("ticket") == ticket.id for e in signed) \
            else "unverified"
    v = (ticket.meta.get("gates") or {}).get("verify") or {}
    if v.get("verdict") != "done":
        return "none"
    for e in signed:
        if (e.get("kind") == "verdict" and e.get("ticket") == ticket.id and e.get("verdict") == "done"
                and e.get("verify_at") == v.get("at")):
            return "verified"
    return "unverified"


def unsigned_gates(ws, ticket, signed: list[dict] | None = None) -> list[str]:
    from orch.core.gates import GATE_SECTIONS
    signed = entries(ws) if signed is None else signed
    return [g for g in GATE_SECTIONS if gate_verification(ws, ticket, g, signed) == "unverified"]


def require_signed(ws, ticket, gates=()) -> None:
    """Refuse to let an agent proceed on an approval of `gates`, or on an answered blocking question, that the
    ledger does not back (whatever the ticket file claims about when or how it was approved), or on an approval of
    text that changed since: a signed approval covers the text it was given for, not what the ticket says now."""
    from orch.core.gates import gate_state
    from orch.errors import ValidationError
    signed = entries(ws)
    for gate in gates:
        if gate_state(ticket, gate) == "invalidated" or not _charter_current(ws, ticket, gate, signed):
            raise ValidationError(f"the {gate} of {ticket.id} changed since it was approved",
                                  hint=f"stop and wait: the human approves the {gate} again (an epic's child: the "
                                       f"epic) before the work goes on (`orch wait {ticket.id}`)")
        if gate_verification(ws, ticket, gate, signed) == "unverified":
            raise ValidationError(f"the {gate} approval of {ticket.id} is not in the ledger on this machine",
                                  hint=NOT_SIGNED_HINT)
    for q in ticket.meta.get("questions") or []:
        if (isinstance(q, dict) and q.get("blocking", True)
                and answer_verification(ws, ticket, q, signed) == "unverified"):
            raise ValidationError(f"the answer to {q.get('id')} on {ticket.id} is not in the ledger on this machine",
                                  hint="ask the human to review it with `orch ledger adopt`; if they did not give "
                                       "that answer, they should answer again")


def _charter_current(ws, ticket, gate: str, signed: list[dict]) -> bool:
    """False when the gate counts only through its epic's signed charter and that charter does not list the gate's
    current hash (the child changed since the human approved the epic)."""
    from orch.core.gates import gate_hash
    g = ((ticket.meta.get("gates") or {}).get(gate) or {})
    if not g.get("approved") or not ticket.meta.get("parent"):
        return True
    if any(e.get("kind") == "gate" and e.get("ticket") == ticket.id and e.get("gate") == gate
           and e.get("hash") == g.get("hash") for e in signed):
        return True  # approved on its own
    from orch.core import epics
    epic = epics.parent_epic(ws, ticket)
    if epic is None or epics.gate_coverage(ws, ticket, gate, signed) != "verified":
        return True  # not charter-covered: gate_verification decides
    return epics._covered(ws, epic, ticket, gate, gate_hash(ticket, gate), signed)


# -- adopt: the human reviews decisions this machine's ledger does not hold ------------------------------------

def item_id(item: dict) -> str:
    """The 8-hex prefix the human types to sign `item`: the gate hash for a gate, else a digest of the decision."""
    if item["kind"] == "gate":
        return str(item["hash"]).split(":", 1)[-1][:8]
    return hashlib.sha256(canonical_json({k: item.get(k) for k in SIGNED_FIELDS})).hexdigest()[:8]


# What a displayed adopt item and the item re-derived under the ticket lock must agree on, field by field.
SIGNED_FIELDS = ("ticket", "title", "kind", "gate", "hash", "hash_v", "qid", "answer", "question_hash", "verdict", "verify_at",
                 "text")


def _provenance(events, tid: str, kinds: tuple, match) -> str:
    for e in reversed(events):
        if e.ticket == tid and e.kind in kinds and match(e):
            return f"events.jsonl: {e.kind} by {e.actor} via {e.via} at {e.at}"
    return "events.jsonl: no recorded decision"


def unsigned_items(ws, tickets) -> list[dict]:
    """Every current decision on `tickets` that this machine's ledger does not hold: approved gates (with their gated
    text in the hash version they were approved with), answered questions, done verdicts and closes."""
    from orch.core.events import read_events
    from orch.core.gates import gate_hash, normalized_text
    from orch.core.questions import question_hash
    signed, events, out = entries(ws), read_events(ws), []
    for t in tickets:
        for gate in unsigned_gates(ws, t, signed):
            g = t.meta["gates"][gate]
            version = next((int(v) for v in (g.get("hash_v"), 2, 1)
                            if v and str(v).isdigit() and gate_hash(t, gate, int(v)) == g.get("hash")), None)
            out.append({"ticket": t.id, "title": t.title, "kind": "gate", "gate": gate, "hash": g.get("hash"),
                        "hash_v": version, "text": normalized_text(t, gate, version) if version else None,
                        "provenance": _provenance(events, t.id, ("gate.approved", "ledger.adopted"),
                                                  lambda e, h=g.get("hash"): e.data.get("hash") == h)})
        for q in t.meta.get("questions") or []:
            if isinstance(q, dict) and answer_verification(ws, t, q, signed) == "unverified":
                out.append({"ticket": t.id, "title": t.title, "kind": "answer", "qid": q.get("id"),
                            "answer": q.get("answer"), "question_hash": question_hash(q),
                            "text": f"{q.get('id')}: {q.get('text')}\nanswer: {q.get('answer')}",
                            "provenance": _provenance(events, t.id, ("question.answered",),
                                                      lambda e, i=q.get("id"): e.data.get("qid") == i)})
        if t.status == "done":
            v = (t.meta.get("gates") or {}).get("verify") or {}
            if v.get("verdict") == "done":
                if done_verification(ws, t, closed=False, signed=signed) == "unverified":
                    out.append({"ticket": t.id, "title": t.title, "kind": "verdict", "verdict": "done",
                                "verify_at": v.get("at"), "text": f"verdict done at {v.get('at')}",
                                "provenance": _provenance(events, t.id, ("verdict.given",),
                                                          lambda e: e.data.get("verdict") == "done")})
            elif done_verification(ws, t, closed=True, signed=signed) == "unverified":
                out.append({"ticket": t.id, "title": t.title, "kind": "close", "text": "closed (done without a verdict)",
                            "provenance": _provenance(events, t.id, ("ticket.moved",),
                                                      lambda e: e.data.get("command") == "close")})
    for item in out:
        item["id"] = item_id(item)
    return out
