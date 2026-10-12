"""``python -I -m orch.records`` (also ``orch records``): the one-shot person-side records tool (H11e).

    orch records init           [--workspace ID] [--json]
    orch records commit         [--workspace ID] [--push] [--fresh-history] [--json]
    orch records push           [--workspace ID] [--remote NAME] [--initial [--yes]] [--json]
    orch records status         [--workspace ID] [--remote NAME] [--json]
    orch records sync-index     [--workspace ID] [--json]
    orch records remote-private [--workspace ID] [--remote NAME] [--off] [--yes] [--json]
    orch records auto on        [--workspace ID] [--push] [--every MIN] [--dev] [--json]   (terminal only)
    orch records auto off       [--workspace ID] [--json]
    orch records auto           [--state DIR] [--json]                                     (the timer's run)

**Mode.** Host mode when the person's own registry names the workspace (``records.*`` ops on the pinned socket,
column P, owner or maintainer); otherwise direct mode (D-P2-38, POSIX only) on the workspace found here: the tool takes
the snapshot under the store lock itself, with the same rules and files, and refuses (``host.required``) once a host
serves it. A call under ``ORCH_GRANT`` is refused (agents never commit or push records through orch); Windows is
refused (``records.unsupported``).

**One run.** One run per workspace at a time (``records.busy``), 60 s for the whole run, git and host calls together
(``records.budget``). Every run is logged to ``<state>/person/records.jsonl`` (start, commit, push, refusal codes;
never contents or credentials).

**Terminal only:** ``auto on`` (asks on ``/dev/tty``), ``push --initial`` and ``remote-private`` (ask on ``/dev/tty``
unless ``--yes``). The dashboard runs only ``commit [--push]`` and ``auto off`` as a fixed-argv child.

``remote-private`` marks the **one exact effective push URL** of the remote as read only by the owner. The mark binds
that URL string; the person's own settings (ssh config, credential helpers, DNS) decide how it is reached. orch trusts
that word; a later change of the destination resolves to another URL and is not marked. Without the mark, a workspace
with convenience signing (the flag on, or convenience events in its history) cannot push records
(``sign.convenience_readers``).

**Output** (the dashboard child contract, §12.11): text, or with ``--json`` exactly one JSON line valid against
:data:`CHILD_SCHEMA`: ``{ok:true, commit?, pushed?, url?, detail?}`` or ``{ok:false, code, message, hint?}``; exit 0 on
success, the catalog exit of ``code`` otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import RUN_BUDGET, RecordsError
from .host_link import TRUST_PROMPT, resolve
from .person import RunLog, run_lock
from .tool import Link, ValidatedTips, commit, init_repo, push, push_url

__all__ = ["CHILD_SCHEMA", "main"]

_SHA = {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"}
#: the records child's one JSON line (§12.11)
CHILD_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "ok": {"const": True},
                "commit": _SHA,
                "pushed": {"type": "boolean"},
                "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "detail": {"type": "object"},
            },
            "required": ["ok"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "ok": {"const": False},
                "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$"},
                "message": {"type": "string", "maxLength": 400},
                "hint": {"type": "string", "maxLength": 400},
            },
            "required": ["ok", "code", "message"],
            "additionalProperties": False,
        },
    ]
}

Confirm = Callable[[str], bool]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch records", description="commit and push orch records")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "commit", "push", "status", "sync-index", "remote-private", "auto"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", help="the workspace id (default: the workspace here)")
        s.add_argument("--state", help="the person's state directory (the timer passes it)")
        s.add_argument("--json", action="store_true")
        if name == "commit":
            s.add_argument("--fresh-history", action="store_true", help="make the next commit a root commit")
            s.add_argument("--push", action="store_true", help="push after the commit")
        if name in ("push", "remote-private", "status", "commit"):
            s.add_argument("--remote", default="origin")
        if name == "push":
            s.add_argument("--initial", action="store_true", help="create the remote branch (terminal only)")
        if name in ("push", "remote-private"):
            s.add_argument("--yes", action="store_true", help="do not ask (you have read the warning)")
        if name == "remote-private":
            s.add_argument("--off", action="store_true", help="remove the mark")
        if name == "auto":
            s.add_argument("switch", nargs="?", choices=["on", "off"])
            s.add_argument("--push", action="store_true", help="also push (auto on)")
            s.add_argument("--every", type=int, default=15, help="minutes between commits, 5 to 1440 (auto on)")
            s.add_argument("--dev", action="store_true", help="allow a source checkout as the timer's program")
    return p


def _tty_confirm(text: str) -> bool:
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(text + "\nType yes to continue: ")
            tty.flush()
            return tty.readline().strip() == "yes"
    except OSError:
        return False


def _flat(res: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    for k in ("commit", "pushed", "url"):
        if res.get(k) is not None:
            out[k] = res[k]
    detail = {k: v for k, v in res.items() if k not in out}
    if detail:
        out["detail"] = detail
    return out


class _Seam:
    """The test seam's resolved workspace (an in-process host connection or a direct link, a given root)."""

    mode = "seam"

    def __init__(self, s: dict[str, Any]) -> None:
        self.workspace_id, self.link, self.root = s["workspace_id"], s["link"], Path(s["root"])
        self.tips = s.get("tips") or ValidatedTips(Path(s["state"]) / "records.json")
        self.state = Path(s.get("state") or self.tips.path.parent)

    def close(self) -> None:
        return None


class _Run:
    """One run's context: the arguments, the seams, the deadline, the confirmation."""

    def __init__(self, args: argparse.Namespace, env: Mapping[str, str], seams: dict[str, Any]) -> None:
        self.args, self.env, self.seams = args, env, seams
        self.deadline = time.monotonic() + RUN_BUDGET
        self.protocols = seams.get("protocols")
        self.confirm: Confirm = seams.get("confirm") or _tty_confirm

    def open(self, workspace: str | None, root: Path | None = None) -> Any:
        s = self.seams
        if s.get("link") is not None and s.get("root") is not None and s.get("workspace_id") is not None:
            if workspace not in (None, s["workspace_id"]):
                raise RecordsError("records.unsupported", "this test seam serves one workspace")
            return _Seam(s)
        state = Path(self.args.state) if self.args.state else None
        return resolve(self.env, workspace, state=state, root=root, deadline=self.deadline)

    def repo(self, root: Path, remote: str | None = None) -> Any:
        from .git import ALLOWED_PROTOCOLS, open_repo

        return open_repo(
            root, env=self.env, protocols=self.protocols or ALLOWED_PROTOCOLS, remote=remote, deadline=self.deadline
        )


def _note(link: Link, args: dict[str, Any]) -> str | None:
    """``records.note`` is a display record: its failure never fails the run (the code goes to ``detail``)."""
    try:
        link("records.note", args)
        return None
    except RecordsError as exc:
        return exc.code


def _auto_entry(tips: Any, wid: str) -> dict[str, Any] | None:
    a = tips.auto(wid)
    return {"on": True, "every_min": a["every_min"], "push": bool(a.get("push"))} if a else None


def _do_commit(run: _Run, r: Any, *, push_after: bool, fresh: bool, remote: str) -> dict[str, Any]:
    repo = run.repo(r.root, remote if push_after else None)
    res = commit(repo, r.link, r.workspace_id, fresh=fresh)
    state = "index_differs" if res.get("index") == "differs" else "ok"
    note = {"manifest": res["manifest"], "commit": res["commit"], "pushed": False, "repo": {"state": state}}
    note["auto"] = _auto_entry(r.tips, r.workspace_id)
    failed = _note(r.link, note)
    if failed:
        res["note"] = failed
    res["pushed"] = False
    if push_after:
        pres = push(repo, r.link, r.workspace_id, r.tips, remote=remote)
        res["pushed"] = bool(pres.get("pushed"))
        res["url"] = pres["url"]
        res["push"] = {k: v for k, v in pres.items() if k not in ("commit", "url", "pushed")}
        if res["pushed"]:
            _note(r.link, {"commit": pres["commit"], "pushed": True, "url": pres["url"]})
    return res


def _do_push(run: _Run, r: Any) -> dict[str, Any]:
    a = run.args
    repo = run.repo(r.root, a.remote)
    if a.initial and not a.yes and not run.confirm(f"Create the records branch on the remote {a.remote}?"):
        raise RecordsError("records.unconfirmed", "not confirmed; nothing pushed (push --initial is terminal only)")
    res = push(repo, r.link, r.workspace_id, r.tips, remote=a.remote, initial=a.initial)
    if res.get("pushed"):
        failed = _note(r.link, {"commit": res["commit"], "pushed": True, "url": res["url"]})
        if failed:
            res["note"] = failed
    return res


def _do_status(run: _Run, r: Any) -> dict[str, Any]:
    from .tool import EMPTY_TREE, _index_tree, _read_index

    note: dict[str, Any] = {"pushed": False, "auto": _auto_entry(r.tips, r.workspace_id)}
    repo_state: dict[str, Any]
    if not os.path.lexists(Path(r.root) / ".git"):
        repo_state = {"state": "missing"}
    else:
        try:
            repo = run.repo(r.root, run.args.remote)
            head = repo.resolve("HEAD")
            head_tree = repo.out("rev-parse", f"{head}^{{tree}}") if head else EMPTY_TREE
            same = _index_tree(repo, _read_index(repo)) == head_tree
            repo_state = {"state": "ok" if same else "index_differs"}
            try:
                note["url"] = push_url(repo, run.args.remote)
            except RecordsError:
                pass
        except RecordsError as exc:
            kind = "not_dedicated" if exc.code == "records.not_dedicated" else "unsafe"
            repo_state = {"state": kind, "problem": exc.message[:300]}
    note["repo"] = repo_state
    _note(r.link, note)
    return dict(r.link("records.status", {}))


def _do_sync_index(run: _Run, r: Any) -> dict[str, Any]:
    from .tool import _read_index, _refresh_index

    repo = run.repo(r.root)
    head = repo.resolve("HEAD")
    if head is None:
        raise RecordsError("records.git_state", "the records branch has no commit yet")
    if not _refresh_index(repo, _read_index(repo), head):
        raise RecordsError("records.busy", "git holds index.lock; try again")
    _note(r.link, {"pushed": False, "repo": {"state": "ok"}})
    return {"index": "synced", "commit": head}


def _do_remote_private(run: _Run, r: Any) -> dict[str, Any]:
    a = run.args
    if a.off:
        return dict(r.link("records.remote_private", {"on": False}))
    repo = run.repo(r.root, a.remote)
    url = push_url(repo, a.remote)
    if not a.yes and not run.confirm(TRUST_PROMPT.format(url=url)):
        raise RecordsError("records.unconfirmed", "not confirmed; nothing changed")
    return dict(r.link("records.remote_private", {"url": url, "on": True}))


def _timer(run: _Run, state: Path) -> Any:
    from .timer import TimerManager, plan

    factory = run.seams.get("timer")
    if factory is not None:
        return factory(state)
    return TimerManager(plan(run.env, state, dev=bool(getattr(run.args, "dev", False))))


def _do_auto_switch(run: _Run, r: Any) -> dict[str, Any]:
    a = run.args
    if a.switch == "on":
        text = (
            f"Turn on automatic records commits every {a.every} min"
            + (" and pushes" if a.push else "")
            + " for this workspace (a timer of your service manager runs orch records auto)."
        )
        if not run.confirm(text):
            raise RecordsError("records.unconfirmed", "not confirmed; auto mode is terminal only")
        entry: dict[str, Any] = {"on": True, "every_min": a.every, "push": bool(a.push)}
        if getattr(r, "mode", "") == "direct":
            entry["root"] = str(r.root)
        r.tips.set_auto(r.workspace_id, entry)  # validates --every before the timer is touched
        try:
            manager = _timer(run, Path(r.state))
            manager.start(changed=manager.install())
        except BaseException:
            r.tips.set_auto(r.workspace_id, None)
            raise
        _note(r.link, {"pushed": False, "auto": _auto_entry(r.tips, r.workspace_id)})
        return {"auto": {"on": True, "every_min": a.every, "push": bool(a.push)}}
    r.tips.set_auto(r.workspace_id, None)
    timer = "kept"
    if not r.tips.autos():
        try:
            _timer(run, Path(r.state)).remove()
            timer = "removed"
        except RecordsError as exc:
            timer = exc.code
    _note(r.link, {"pushed": False, "auto": None})
    return {"auto": None, "timer": timer}


def _due(status: dict[str, Any], every_min: int, now: float) -> bool:
    import calendar

    last = status.get("last") or {}
    at = last.get("at")
    if not at:
        return True
    try:
        t = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return True
    return now - t >= every_min * 60


def _run_auto_all(args: argparse.Namespace, env: Mapping[str, str], seams: dict[str, Any]) -> tuple[int, dict]:
    """The timer's run: every workspace with auto on, one after the other, each in its own lock and budget. Commits
    when events are pending and the last commit is older than ``every_min``; pushes when ``push`` is on."""
    from orch.ops.runtime import state_dir

    from .person import PersonState

    state = Path(args.state) if args.state else Path(seams["state"]) if seams.get("state") else state_dir(env)
    tips = seams.get("tips") or PersonState(state / "records.json")
    clock = seams.get("clock") or time.time
    log = RunLog(state / "person" / "records.jsonl", clock)
    runs: list[dict[str, Any]] = []
    worst = 0
    for wid, entry in sorted(tips.autos().items()):
        run = _Run(args, env, seams)
        item: dict[str, Any] = {"workspace": wid}
        try:
            r = run.open(wid, Path(entry["root"]) if entry.get("root") else None)
            try:
                with run_lock(Path(r.state), wid):
                    log.write(wid, "start", cmd="auto")
                    status = r.link("records.status", {})
                    pending = int((status.get("pending") or {}).get("events") or 0)
                    if pending == 0:
                        item["skipped"] = "nothing pending"
                    elif not _due(status, int(entry["every_min"]), clock()):
                        item["skipped"] = "not due"
                    else:
                        res = _do_commit(run, r, push_after=bool(entry.get("push")), fresh=False, remote="origin")
                        item.update(commit=res["commit"], pushed=res["pushed"], url=res.get("url"))
                        log.write(wid, "auto", commit=res["commit"], pushed=res["pushed"], url=res.get("url"))
            finally:
                r.close()
        except RecordsError as exc:
            item.update(code=exc.code, message=exc.message)
            log.write(wid, "refused", cmd="auto", code=exc.code)
            worst = worst or exc.exit
        runs.append(item)
    return worst, {"runs": runs}


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    *,
    link: Link | None = None,
    root: Path | None = None,
    workspace_id: str | None = None,
    tips: ValidatedTips | None = None,
    protocols: str | None = None,
    confirm: Confirm | None = None,
    timer: Callable[[Path], Any] | None = None,
    state: Path | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """The CLI. The keyword arguments are seams for tests (an in-process host connection, local bare repos, a fake
    timer, a confirmation); production resolves everything from the person's registry and state directory."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    seams = {
        "link": link, "root": root, "workspace_id": workspace_id, "tips": tips, "protocols": protocols,
        "confirm": confirm, "timer": timer, "state": state, "clock": clock,
    }  # fmt: skip

    def emit(doc: dict[str, Any], text: str) -> None:
        out.write((json.dumps(doc, sort_keys=True) if args.json else text) + "\n")

    def fail(exc: RecordsError) -> int:
        doc = {"ok": False, "code": exc.code, "message": exc.message[:400]}
        if exc.hint:
            doc["hint"] = exc.hint[:400]
        emit(doc, f"{exc.code}: {exc.message}" + (f"\n  {exc.hint}" if exc.hint else ""))
        return exc.exit

    try:
        if os.name == "nt":
            raise RecordsError("records.unsupported", "the records tool does not run on Windows yet (D-P2-38)")
        if env.get("ORCH_GRANT"):
            raise RecordsError("op.not_allowed", "agents do not commit or push records through orch", exit_=3)
        if args.cmd == "auto" and args.switch is None:
            worst, detail = _run_auto_all(args, env, seams)
            emit({"ok": True, "detail": detail}, json.dumps(detail, sort_keys=True))
            return worst
        run = _Run(args, env, seams)
        r = run.open(args.workspace)
        log = RunLog(Path(r.state) / "person" / "records.jsonl", clock or time.time)
        try:
            with run_lock(Path(r.state), r.workspace_id):
                log.write(r.workspace_id, "start", cmd=args.cmd)
                try:
                    res, text = _dispatch(run, r)
                except RecordsError as exc:
                    log.write(r.workspace_id, "refused", cmd=args.cmd, code=exc.code)
                    raise
                log.write(
                    r.workspace_id, args.cmd, commit=res.get("commit"), pushed=res.get("pushed"), url=res.get("url")
                )
        finally:
            r.close()
        emit(_flat(res), text)
        return 0
    except RecordsError as exc:
        return fail(exc)
    except Exception as exc:  # noqa: BLE001 - never a traceback to the dashboard: one JSON line
        code = getattr(exc, "code", None) or "internal"
        message = str(getattr(exc, "message", None) or type(exc).__name__)
        return fail(RecordsError(str(code), message, hint=getattr(exc, "hint", None), exit_=1))


def _dispatch(run: _Run, r: Any) -> tuple[dict[str, Any], str]:
    a = run.args
    if a.cmd == "init":
        from .git import pinned_root

        res = init_repo(pinned_root(r.root), env=run.env)
        run.repo(r.root)  # the result must be a usable dedicated repo
        return res, "records repo ready" + (" (created)" if res["created"] else "")
    if a.cmd == "commit":
        res = _do_commit(run, r, push_after=a.push, fresh=a.fresh_history, remote=a.remote)
        text = (
            f"committed {res['commit'][:12]} (ws_seq {res['ws_seq']}, index {res['index']})"
            if res["changed"]
            else f"nothing to commit ({res['commit'][:12]})"
        )
        if a.push:
            text += f"; pushed to {res['url']}" if res["pushed"] else "; remote up to date"
        return res, text
    if a.cmd == "push":
        res = _do_push(run, r)
        return res, (f"pushed {res['commit'][:12]} to {res['url']}" if res["pushed"] else "up to date")
    if a.cmd == "status":
        res = _do_status(run, r)
        return {"status": res}, json.dumps(res, sort_keys=True)
    if a.cmd == "sync-index":
        res = _do_sync_index(run, r)
        return res, f"index synced to {res['commit'][:12]}"
    if a.cmd == "remote-private":
        res = _do_remote_private(run, r)
        if a.off:
            return res, "private-remote mark removed"
        url = (res.get("private_remote") or {}).get("url", "")
        return res, f"marked {url} private: {res.get('text', '')}"
    res = _do_auto_switch(run, r)
    return res, ("auto mode on" if a.switch == "on" else "auto mode off")
