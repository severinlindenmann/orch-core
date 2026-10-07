from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from orch.core import store
from orch.core.ops import Ops
from orch.dashboard.routes_ticket import load_or_error
from orch.dashboard.views import HUMAN, back, confirm_page, error_text, page, safe_next
from orch.errors import OrchError, UsageError

router = APIRouter()


def _ops(request: Request) -> Ops:
    return Ops(request.app.state.ws, HUMAN)


def _ticket_url(request: Request, ref: str) -> str:
    try:
        return f"/t/{store.resolve(request.app.state.ws, ref).id}"
    except UsageError:
        return "/"


# Optional form field: where to return after the action (e.g. Decisions sends "/?kind=questions").
# Anything but a safe same-origin path is ignored and the ticket page is used, as before.
Next = Annotated[str, Form(alias="next")]


def _run(request: Request, ref: str, action, success: str, next_url: str = ""):
    url = safe_next(next_url) or _ticket_url(request, ref)
    try:
        action()
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=success)


@router.post("/t/{ref}/approve")
def approve(request: Request, ref: str, gate: Annotated[str, Form()], seen: Annotated[str, Form()] = "",
            next_url: Next = "", despite_open_question: Annotated[str, Form()] = "",
            delegate: Annotated[str, Form()] = "", max_children: Annotated[str, Form()] = "",
            max_size: Annotated[str, Form()] = "", factory: Annotated[str, Form()] = "",
            dark: Annotated[str, Form()] = "", confirm_dark: Annotated[str, Form()] = "",
            start: Annotated[str, Form()] = "", release: Annotated[str, Form()] = "",
            rollback: Annotated[str, Form()] = "", confirm_production: Annotated[str, Form()] = "",
            close: Annotated[str, Form()] = ""):
    """`seen` is the hash of what the page showed (for an epic: its charter). `delegate` (epics, the checkbox in
    the confirm) opts in to delegation with `max_children` / `max_size`; Ops.approve checks the rest. `factory`
    (epics, Start as AI Factory) signs the factory charter with the factory's own limits (D5/D6) and wins over
    `delegate`; Ops refuses it while factory.enabled is off, and for any process under an agent harness. `dark`
    (Start as a Dark AI Factory) signs the same factory charter with `dark: True` and needs the word dark typed in
    `confirm_dark`, as on the New ticket page; Ops refuses it while Dark is off."""
    if not seen:
        url = safe_next(next_url) or _ticket_url(request, ref)
        return back(url, err="reload the page and review again")
    despite = despite_open_question in ("1", "on", "true")
    if start in ("factory", "dark"):  # the epic page's Start radios (None / AI Factory / Dark)
        factory, dark = "1", ("1" if start == "dark" else "")  # the radio decides, nothing else posted
    if dark not in ("1", "on", "true") and (close in ("1", "on", "true") or rollback in ("1", "on", "true")):
        return back(safe_next(next_url) or _ticket_url(request, ref),
                    err="only a Dark AI Factory closes the epic by itself or signs a rollback: nothing was signed")
    if dark not in ("1", "on", "true") and release not in ("", "none"):  # refused, never silently dropped
        return back(safe_next(next_url) or _ticket_url(request, ref),
                    err="only a Dark AI Factory signs a release: nothing was signed")
    if dark in ("1", "on", "true") and confirm_dark.strip() != "dark":
        return back(safe_next(next_url) or _ticket_url(request, ref),
                    err="type dark to start a Dark AI Factory: nothing was signed")
    if dark in ("1", "on", "true"):
        from orch.dashboard.routes_new import release_problem
        roll = rollback in ("1", "on", "true")
        why = release_problem(request.app.state.ws, release, roll, confirm_production)
        if why:
            return back(safe_next(next_url) or _ticket_url(request, ref), err=f"{why}: nothing was signed")
        limits = {"factory": True, "dark": True}
        if release in ("merge", "dev", "prod"):  # "Release up to" (a Dark start only); Ops checks the recipe
            limits["release"] = release
        if roll:
            limits["rollback"] = True
        if close in ("1", "on", "true"):  # the auto-close checkbox (a Dark start only): replaces the verdict
            limits["close"] = True
    elif factory in ("1", "on", "true"):
        limits = {"factory": True}
    elif delegate in ("1", "on", "true"):
        limits = {"max_children": max_children.strip() or None, "max_size": max_size.strip() or None}
    else:
        limits = None

    def action():
        if limits and limits.get("factory"):
            start_factory(request.app.state.ws, ref, seen, limits, despite, gate=gate)
        else:
            _ops(request).approve(ref, gate, expected_hash=seen, despite_open_question=despite, delegate=limits)

    return _run(request, ref, action, f"{gate} approved", next_url)


def start_factory(ws, ref: str, seen: str, limits: dict, despite: bool = False, *, gate: str = "requirements"):
    """The dashboard's factory start (the epic page and the New ticket page): the human's signed charter approval with
    the factory's limits (`dark` too, for a Dark one), then the runner is armed for that delegation. Only this start
    lets the runner work for an epic."""
    epic = Ops(ws, HUMAN).approve(ref, gate, expected_hash=seen, despite_open_question=despite, delegate=limits)
    _arm_runner(ws, epic)
    return epic


def _arm_runner(ws, epic) -> None:
    from orch.core import epics, factory_sessions
    d = epics.delegation(ws, epic)
    if d and d.get("factory"):
        factory_sessions.arm(ws, HUMAN, d["id"])


@router.post("/t/{ref}/approve-together")
def approve_together(request: Request, ref: str, seen: Annotated[str, Form()] = "",
                     seen_plan: Annotated[str, Form()] = "", next_url: Next = "",
                     despite_open_question: Annotated[str, Form()] = ""):
    """F2: requirements and plan in one confirm. `seen` and `seen_plan` are the hashes of the two texts the page
    showed in full; Ops.approve_together checks each gate as a single approval would."""
    if not seen or not seen_plan:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    despite = despite_open_question in ("1", "on", "true")
    return _run(request, ref, lambda: _ops(request).approve_together(ref, requirements_hash=seen, plan_hash=seen_plan,
                                                                     despite_open_question=despite),
                "requirements and plan approved", next_url)


@router.post("/t/{ref}/epic/pause")
def epic_pause(request: Request, ref: str, next_url: Next = ""):
    return _run(request, ref, lambda: _ops(request).epic_pause(ref), "delegation paused", next_url)


@router.post("/t/{ref}/request-changes")
def request_changes(request: Request, ref: str, gate: Annotated[str, Form()], message: Annotated[str, Form()] = "",
                     seen: Annotated[str, Form()] = "", next_url: Next = ""):
    # Bound to the gate text the human read (like approve): a request about text that changed since is refused.
    if not seen:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    return _run(request, ref, lambda: _ops(request).request_changes(ref, gate, message, expected_hash=seen),
                f"asked for changes on {gate}", next_url)


@router.post("/t/{ref}/answer")
def answer(request: Request, ref: str, qid: Annotated[str, Form()],
           value: Annotated[list[str], Form()] = [], note: Annotated[str, Form()] = "",  # FastAPI copies the default
           qhash: Annotated[str, Form()] = "", next_url: Next = ""):
    # Bound to the question the human read (question_hash): an agent that re-asks it with other text or options
    # while the answer waits for its Undo window gets no answer to the new question.
    if not qhash:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    joined = ",".join(v for v in value if v.strip()) if len(value) > 1 else (value[0] if value else "")
    return _run(request, ref, lambda: _ops(request).answer(ref, qid, joined, note=note or None, expected_hash=qhash),
                f"answered {qid.upper()}", next_url)


@router.post("/t/{ref}/verdict")
def verdict(request: Request, ref: str, verdict: Annotated[str, Form()], message: Annotated[str, Form()] = "",
            next_url: Next = "", seen: Annotated[str, Form()] = "", skip_release: Annotated[str, Form()] = ""):
    """`seen` (required): the hash of the criteria and evidence the page showed (orch.core.epics.verdict_hash; for
    an epic over its open children). Ops.verdict refuses it once they changed. `skip_release`: the reason of "Close
    without releasing" (an epic whose signed release has not run); Ops.verdict refuses such an epic without it."""
    if not seen:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    return _run(request, ref, lambda: _ops(request).verdict(ref, verdict, message or None, expected_hash=seen,
                                                             skip_release=skip_release or None),
                "closed without releasing" if skip_release.strip() else f"verdict {verdict}", next_url)


@router.post("/t/{ref}/move")
def move(request: Request, ref: str, to: Annotated[str, Form()], next_url: Next = ""):
    ops = _ops(request)
    url = safe_next(next_url) or _ticket_url(request, ref)
    try:
        ops.move(ref, to)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg="; ".join([f"moved to {to}", *ops.warnings]))


@router.post("/t/{ref}/release")
def release(request: Request, ref: str, next_url: Next = "", ask: Annotated[str, Form()] = ""):
    if ask:  # posted without JS from a dialog-tier form: confirm on a page first
        try:
            entry = store.resolve(request.app.state.ws, ref)
        except UsageError as e:
            return back(safe_next(next_url) or "/", err=error_text(e))
        harness = (entry.meta or {}).get("claim", {}).get("harness") if isinstance((entry.meta or {}).get("claim"), dict) else None
        return confirm_page(request, action=f"/t/{entry.id}/release", fields=[("next", next_url)],
                            title=f"Release {harness or 'the agent'}'s claim on {entry.id}?",
                            body=f"{entry.id} goes back to open and any agent can claim it. Branch and notes stay.",
                            confirm="Release claim", cancel="Keep claim",
                            cancel_href=safe_next(next_url) or f"/t/{entry.id}", nav="today")
    return _run(request, ref, lambda: _ops(request).release(ref), "claim released", next_url)


@router.post("/t/{ref}/comment")
def comment(request: Request, ref: str, text: Annotated[str, Form()], next_url: Next = ""):
    return _run(request, ref, lambda: _ops(request).log(ref, text), "comment added", next_url)


def _sanitized_name(name: str) -> str:
    from orch.core.ops import _UNSAFE_NAME  # same sanitising Ops.artifact_add applies

    return _UNSAFE_NAME.sub("-", name).split("/")[-1].strip() or "upload"


def _free_name(ws, tid: str, name: str) -> str:
    """Pick `name`, or the next "stem-2.ext", "stem-3.ext", ... not already stored for this ticket."""
    fname = _sanitized_name(name)
    dest_dir = ws.artifacts_dir / tid
    if not (dest_dir / fname).exists():
        return name
    stem, ext = Path(fname).stem, Path(fname).suffix
    n = 2
    while (dest_dir / f"{stem}-{n}{ext}").exists():
        n += 1
    return f"{stem}-{n}{ext}"


def add_uploads(ws, ops: Ops, ref: str, files: list[UploadFile]) -> list[Path]:
    """Store uploaded files as artifacts via Ops (through orchestrator/temporary/); returns their paths.

    A name that collides with an existing artifact (or an earlier file in this same batch) gets
    the next free "stem-2.ext", "stem-3.ext", ... name instead of failing the whole request.
    """
    added = []
    tid = store.resolve(ws, ref).id
    for f in files:
        if not f.filename:
            continue  # an empty file input still sends one nameless part
        name = Path(f.filename.replace("\\", "/")).name or "upload"
        name = _free_name(ws, tid, name)
        fd, tmp = tempfile.mkstemp(dir=ws.temporary_dir, prefix=".upload-")
        try:
            with open(fd, "wb") as out:
                shutil.copyfileobj(f.file, out)
            added.append(ops.artifact_add(ref, Path(tmp), name=name))
        finally:
            Path(tmp).unlink(missing_ok=True)
    return added


@router.post("/t/{ref}/artifacts")
def upload(request: Request, ref: str, files: Annotated[list[UploadFile], File()]):
    ws = request.app.state.ws
    return _run(request, ref, lambda: add_uploads(ws, _ops(request), ref, files), f"{len(files)} file(s) added")


@router.get("/t/{ref}/edit")
def edit_form(request: Request, ref: str):
    ws, path, t, error = load_or_error(request, ref)
    if error:
        return error
    return page(request, "edit.html", nav="board", title=f"Edit {t.id}", tid=t.id, text=path.read_text(encoding="utf-8"),
                mtime=path.stat().st_mtime_ns, problem=None)


@router.post("/t/{ref}/edit")
def edit_save(request: Request, ref: str, text: Annotated[str, Form()], mtime: Annotated[str, Form()] = ""):
    try:
        expected = int(mtime) if mtime else None
        t = _ops(request).replace_raw(ref, text.replace("\r\n", "\n"), expected)
    except (OrchError, ValueError) as e:
        message = error_text(e) if isinstance(e, OrchError) else "invalid form data"
        tid = _ticket_url(request, ref).removeprefix("/t/")
        return page(request, "edit.html", 409, nav="board", title=f"Edit {tid}", tid=tid, text=text, mtime=mtime, problem=message)
    return back(f"/t/{t.id}", msg="file saved")


_TASK_ACTIONS = {"done": "done", "skip": "skipped", "reopen": "reopened"}


@router.post("/t/{ref}/task")
def task_action(request: Request, ref: str, task: Annotated[str, Form()], action: Annotated[str, Form()],
                message: Annotated[str, Form()] = "", next_url: Next = ""):
    ops = _ops(request)
    run = {"done": lambda: ops.task_done(ref, task, message or None),
           "skip": lambda: ops.task_skip(ref, task, message),
           "reopen": lambda: ops.task_reopen(ref, task, message or None)}.get(action)
    if run is None:
        return back(safe_next(next_url) or _ticket_url(request, ref), err=f"unknown task action {action!r}")
    return _run(request, ref, run, f"{task.upper()} {_TASK_ACTIONS[action]}", next_url)


@router.post("/t/{ref}/task/add")
def task_add(request: Request, ref: str, text: Annotated[str, Form()], owner: Annotated[str, Form()] = "agent",
             next_url: Next = ""):
    return _run(request, ref, lambda: _ops(request).task_add(ref, [{"text": text, "owner": owner}]), "task added", next_url)
