"""AI Factory phase 4 (#2, #31): what only the runner may write, beside the ledger and outside the repository.

- A **session binding** ties one agent session the runner launched to (epic, delegation, child). The permission hook
  trusts only a binding to decide which epic's grants apply: a claim in a ticket, an environment variable or a
  session id an agent chose gives no factory treatment. The runner generates the session id, writes the binding
  exclusively *before* the agent starts, and only a human process (the dashboard server the human started) may write
  one. The guard keeps agents away from the folder, as from the rest of `permits/`.
- An **armed** marker says the human started this delegation from the dashboard: the runner works only for armed,
  active delegations. Re-approving the epic is a new delegation and arms nothing by itself.
- A **run** marker counts one launch of one child (a cap on launches, and on how many children one delegation may
  start, that editing tickets or events cannot lower), or of the delegation's planner (its own key and cap).

Every reader fails closed: a missing, unreadable, malformed or foreign record is "not bound / not armed".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from pathlib import Path

from orch.errors import HumanOnlyError, ValidationError

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_LAUNCHES = 5  # per child and delegation: a child that keeps parking is the human's to look at
PLANNER_LAUNCHES = 2  # per delegation: a planner that ends twice without children is the human's to look at
_MAX_BYTES = 4096
# `checkout`: the checkout id (orch.core.ledger.checkout_id) of the workspace the runner launched from, and `start`
# the directory it started the session in; the hook takes the Dark switch and profile from this checkout, never from
# the agent's own working directory or `.git` file.
_KEYS = {"workspace", "checkout", "start", "session", "epic", "delegation", "child", "name", "wake", "pid", "pid_start",
         "at"}


def _root() -> Path:
    from orch.core.ledger import base_dir
    return base_dir() / "permits"


def human_check(actor, what: str) -> None:
    from orch.core.lifecycle import require_human
    if not actor.is_human:
        raise HumanOnlyError(f"{what} is a human-only action")
    require_human(actor, what)


def new_session_id() -> str:
    """A fresh UUID (the form the harness takes) from the system's random source: 122 random bits, a bearer secret
    until the hook has seen it. The runner writes it to no event, ticket, log line or page; the agent's own `orch
    claim` records it in the claimed ticket (docs/factory.md, "Session binding"), which grants nothing elsewhere."""
    return str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))


def _create(path: Path, body: dict | None = None) -> bool:
    """Create `path` exclusively (parents as needed); False when it exists already."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if body is not None:
            json.dump(body, f, ensure_ascii=True)
    return True


# -- session bindings -------------------------------------------------------------------------------------------------

def bind(ws, actor, *, session: str, epic: str, delegation: str, child: str, name: str, wake: str = "",
         start: str = "") -> dict:
    """Human only (the runner, in the dashboard the human started): bind `session` to its epic, delegation and child,
    and to this workspace's checkout. Exclusive: a session id is bound once."""
    from orch.clock import stamp_s
    from orch.core.ledger import checkout_id, workspace_id
    human_check(actor, "binding a factory session")
    if not isinstance(session, str) or not SESSION_ID.match(session):
        raise ValidationError("a factory session id is a UUID the runner generated")
    body = {"workspace": workspace_id(ws), "checkout": checkout_id(ws), "start": str(start or ws.root),
            "session": session, "epic": str(epic), "delegation": str(delegation),
            "child": str(child), "name": str(name), "wake": str(wake), "pid": "", "pid_start": "", "at": stamp_s()}
    if not _create(_root() / "sessions" / f"{session}.json", body):
        raise ValidationError(f"session {session} is bound already")
    return body


def proc_start(pid: int) -> str | None:
    """When process `pid` started, as `ps` prints it (None when it is gone or `ps` fails). With the pid it tells the
    process the runner started from a later one that reused the number. Tests replace it."""
    import subprocess
    try:
        r = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                           timeout=5, stdin=subprocess.DEVNULL, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    out = " ".join(r.stdout.split())
    return out if r.returncode == 0 and out else None


def set_pid(ws, actor, session: str, pid) -> None:
    """Human only (the runner): record the first process of the session's tmux pane and when it started, once. The
    hook then trusts the binding only for a process that runs under that very process, so a copied session id used
    elsewhere gets nothing."""
    human_check(actor, "binding a factory session")
    b = binding(ws, session)
    start = proc_start(pid) if isinstance(pid, int) and not isinstance(pid, bool) and pid >= 2 else None
    if b is None or b["pid"] or not start:
        raise ValidationError("the session's process cannot be recorded")
    path = _root() / "sessions" / f"{session}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**b, "pid": str(pid), "pid_start": start}, ensure_ascii=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def chain_pids() -> set[int]:
    """The pids of this process's ancestors (tests replace it)."""
    from orch.actor import process_chain
    return {p for p, _ in (process_chain() or [])}


def trusted(ws, session) -> dict | None:
    """The binding for `session` when this process runs under the process the runner recorded for it (same pid, same
    start time), else None: what the permission hook trusts. Compared in constant time; a binding without a recorded
    process is not trusted yet."""
    import hmac
    b = binding(ws, session)
    if b is None or not hmac.compare_digest(b["session"].encode(), str(session).encode()):
        return None
    if not (b["pid"].isdigit() and b["pid_start"] and int(b["pid"]) in chain_pids()):
        return None
    return b if hmac.compare_digest(proc_start(int(b["pid"])) or "", b["pid_start"]) else None


def session_state(ws, session) -> tuple[str, dict | None]:
    """What orch's own limits for factory sessions go by, failing closed: ("trusted", binding) for a session the
    runner bound and this process runs under; ("none", None) only when that is certain: the id is not a session id the
    runner could have made, or no binding record exists for it; ("unknown", None) for anything else: a record that
    exists but does not verify (damaged, another workspace's, another process tree, no process recorded yet) or an
    error while looking."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return "none", None
    try:
        os.lstat(_root() / "sessions" / f"{session}.json")
    except FileNotFoundError:
        return "none", None
    except Exception:
        return "unknown", None
    try:
        b = trusted(ws, session)
    except Exception:
        return "unknown", None
    return ("trusted", b) if b is not None else ("unknown", None)


def _read(ws, path: Path) -> dict | None:
    from orch.core.artifacts import read_regular
    from orch.core.ledger import workspace_id
    raw = read_regular(path, limit=_MAX_BYTES, root=_root())
    if raw is None:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(body, dict) or set(body) != _KEYS or body["workspace"] != workspace_id(ws)
            or not all(isinstance(v, str) for v in body.values()) or not SESSION_ID.match(body["session"])
            or path.name.split(".")[0] != body["session"]):
        return None
    return body


def binding(ws, session) -> dict | None:
    """The binding of a session the runner launched and has not ended, for this workspace; None otherwise."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return None
    return _read(ws, _root() / "sessions" / f"{session}.json")


def _listing(ws, suffix: str) -> list[dict]:
    try:
        names = sorted(os.listdir(_root() / "sessions"))
    except OSError:
        return []
    out = [b for n in names if n.endswith(suffix) and (b := _read(ws, _root() / "sessions" / n)) is not None]
    return sorted(out, key=lambda b: b["at"])


def bindings(ws) -> list[dict]:
    """Every live binding of this workspace, oldest first."""
    return _listing(ws, ".json")


def ended(ws) -> list[dict]:
    """Bindings of sessions that are over, oldest first (`wake`: what the child waited for when it started)."""
    return _listing(ws, ".ended")


def end(ws, session: str, wake: str | None = None) -> None:
    """A session is over (it stopped, or the runner stopped it): its binding stops counting at once. The record is
    kept as an `.ended` file; its `wake` is what the child waited for when it started, so the runner starts the child
    again only when that changed."""
    b = binding(ws, session)
    if b is None:
        return
    src = _root() / "sessions" / f"{session}.json"
    dst = _root() / "sessions" / f"{session}.ended"
    try:
        dst.write_text(json.dumps(b if wake is None else {**b, "wake": wake}, ensure_ascii=True), encoding="utf-8")
    finally:
        try:
            src.unlink()
        except OSError:
            pass


# -- armed delegations ------------------------------------------------------------------------------------------------

def _key(ws, *parts: str) -> str:
    from orch.core.ledger import workspace_id
    return hashlib.sha256("|".join([workspace_id(ws), *parts]).encode("utf-8")).hexdigest()[:32]


def arm(ws, actor, delegation: str) -> None:
    """Human only: the human started this factory delegation from the dashboard, so the runner may work for it."""
    human_check(actor, "arming the factory runner")
    _create(_root() / "armed" / _key(ws, delegation))


def armed(ws, delegation: str) -> bool:
    return (_root() / "armed" / _key(ws, str(delegation))).is_file()


# -- launch markers ---------------------------------------------------------------------------------------------------

def _run_dir() -> Path:
    return _root() / "runs"


def runs(ws, delegation: str, child: str | None = None) -> int:
    """Launches counted so far: of one child, or the number of distinct children started under the delegation. An
    unreadable folder counts as the limit (fail closed)."""
    prefix = _key(ws, delegation)
    try:
        names = [n for n in os.listdir(_run_dir()) if n.startswith(prefix + ".")]
    except FileNotFoundError:
        return 0
    except OSError:
        return 10**9
    if child is not None:
        return sum(1 for n in names if n.split(".")[1] == _safe(child))
    return len({n.split(".")[1] for n in names})


def _safe(child: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", child)


def mark_run(ws, delegation: str, child: str) -> bool:
    """Count one more launch of `child`; False once MAX_LAUNCHES are used. The caller holds the delegation lock."""
    prefix = f"{_key(ws, delegation)}.{_safe(child)}"
    return any(_create(_run_dir() / f"{prefix}.{n}") for n in range(1, MAX_LAUNCHES + 1))


# The planner (one session that splits a childless epic) has markers of its own, under a key no child's marker shares,
# so it never counts as a child against the charter's max_children nor against a child's launches.

def is_planner(b: dict) -> bool:
    """A planner's binding names the epic itself as its `child` (a child binding names a non-epic ticket)."""
    return b["child"] == b["epic"]


def planner_runs(ws, delegation: str) -> int:
    """Planner launches counted so far under the delegation; an unreadable folder counts as the limit."""
    prefix = _key(ws, delegation, "planner") + "."
    try:
        return sum(1 for n in os.listdir(_run_dir()) if n.startswith(prefix))
    except FileNotFoundError:
        return 0
    except OSError:
        return 10**9


def mark_planner_run(ws, delegation: str) -> bool:
    """Count one more planner launch; False once PLANNER_LAUNCHES are used (each marker is created exclusively)."""
    prefix = _key(ws, delegation, "planner")
    return any(_create(_run_dir() / f"{prefix}.{n}") for n in range(1, PLANNER_LAUNCHES + 1))


def unmark_planner_run(ws, delegation: str) -> None:
    """Take back the latest planner launch (the human's dashboard stopped it: that is not one of its launches).
    Only the runner, in the dashboard the human started, calls it; the guard keeps agents away from the folder."""
    prefix = _key(ws, delegation, "planner")
    for n in range(PLANNER_LAUNCHES, 0, -1):
        try:
            (_run_dir() / f"{prefix}.{n}").unlink()
            return
        except FileNotFoundError:
            continue
        except OSError:
            return


# -- runner records: nudges and sessions that ended right after their start -------------------------------------------
# Written only by the runner (a human process), read by the runner and the run view. A record that does not read back
# exactly is treated as spent (no nudge) or absent (nothing shown): fail closed.

_NUDGE_KEYS = {"session", "epic", "delegation", "answers", "count", "last", "pane", "pane_at", "idle"}


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=True)
    os.replace(tmp, path)


def _read_json(path: Path, limit: int = _MAX_BYTES) -> dict | None:
    from orch.core.artifacts import read_regular
    raw = read_regular(path, limit=limit, root=_root())
    try:
        body = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def nudge_record(session: str) -> dict | None:
    """The runner's nudge record of a session, or None (none, or one that does not read back exactly)."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return None
    b = _read_json(_root() / "nudges" / f"{session}.json")
    if b is None or set(b) != _NUDGE_KEYS or b["session"] != session or not isinstance(b["count"], int) \
            or not isinstance(b["answers"], dict) or not all(isinstance(b[k], str) for k in ("epic", "delegation",
                                                                                           "last", "pane", "pane_at",
                                                                                           "idle")):
        return None
    return b


def write_nudge_record(actor, body: dict) -> None:
    """Human only (the runner)."""
    human_check(actor, "recording a factory nudge")
    if set(body) != _NUDGE_KEYS or not SESSION_ID.match(str(body["session"])):
        raise ValidationError("not a nudge record")
    _write_json(_root() / "nudges" / f"{body['session']}.json", body)


def nudges(delegation: str) -> int:
    """How many times the runner typed into sessions of this delegation (the run view's "nudged N times")."""
    try:
        names = os.listdir(_root() / "nudges")
    except OSError:
        return 0
    total = 0
    for n in names:
        b = nudge_record(n[:-5]) if n.endswith(".json") else None
        if b is not None and b["delegation"] == delegation:
            total += max(0, b["count"])
    return total


def at_trust_question(session: str) -> bool:
    """Whether the runner last saw this session's pane at Claude Code's folder-trust question."""
    b = nudge_record(session)
    return b is not None and b["idle"] == "trust"


def idle_since(session: str):
    """Since when the runner last saw this session's pane idle at its prompt and unchanged (a datetime), or None: not
    idle, not watched, or a record that does not read back (unknown is never idle)."""
    from orch import clock
    b = nudge_record(session)
    if b is None or b["idle"] != "1" or not b["pane_at"]:
        return None
    try:
        return clock.parse_stamp(b["pane_at"])
    except ValueError:
        return None


def record_early_end(ws, actor, b: dict, status: str, tail: str) -> None:
    """Human only (the runner): the session of binding `b` ended right after it started. One record per delegation
    and child (the latest wins); `tail` is already escaped and capped by the caller."""
    from orch.clock import stamp_s
    human_check(actor, "recording a factory session's end")
    body = {"epic": b["epic"], "delegation": b["delegation"], "child": b["child"], "status": str(status)[:40],
            "tail": str(tail)[:4000], "at": stamp_s()}
    _write_json(_root() / "early-ends" / f"{_key(ws, b['delegation'], b['child'])}.json", body)


def early_ends(ws, delegation: str) -> list[dict]:
    """The early-end records of a delegation, newest first."""
    try:
        names = os.listdir(_root() / "early-ends")
    except OSError:
        return []
    out = []
    for n in names:
        body = _read_json(_root() / "early-ends" / n, limit=8192) if n.endswith(".json") else None
        if (body is not None and set(body) == {"epic", "delegation", "child", "status", "tail", "at"}
                and all(isinstance(v, str) for v in body.values()) and body["delegation"] == delegation
                and n == f"{_key(ws, delegation, body['child'])}.json"):
            out.append(body)
    return sorted(out, key=lambda x: x["at"], reverse=True)


# -- tickets a bound session created ----------------------------------------------------------------------------------
# One empty file per ticket in permits/sessions/<session>.created/, created exclusively by the session's own orch
# process (`orch new`, Ops.new) once the ticket is saved. Unlike a binding, an agent's process writes it: the guard keeps
# agents' tools and commands away from the permits folder (and a Dark profile rule never matches a command naming it),
# which is the same best effort that keeps them from the bindings. A ticket listed here is one the session may go on
# changing outside its epic (its follow-ups); nothing else outside the epic.

def _created_dir(session: str) -> Path:
    return _root() / "sessions" / f"{session}.created"


def record_created(session: str, ticket_id: str) -> None:
    """Note that `session` created `ticket_id` (exclusive create; an existing note is kept)."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        raise ValidationError("not a runner session id")
    _create(_created_dir(session) / _safe(str(ticket_id)).upper())


def created(session: str, ticket_id: str) -> bool:
    """Whether `session` created `ticket_id` (a regular file only; any doubt is no)."""
    import stat as st
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return False
    try:
        return st.S_ISREG(os.lstat(_created_dir(session) / _safe(str(ticket_id)).upper()).st_mode)
    except OSError:
        return False
