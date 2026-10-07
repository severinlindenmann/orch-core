from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile

from orch.core import store
from orch.core.ops import Ops
from orch.core.constants import SUCCEEDED
from orch.dashboard.data.metrics import RESOLUTION_LABELS
from orch.dashboard.routes_ticket import load_or_error
from orch.dashboard.reach import request_actor
from orch.dashboard.views import back, confirm_page, error_text, page, safe_next
from orch.errors import OrchError, UsageError

router = APIRouter()


def _ops(request: Request) -> Ops:
    return Ops(request.app.state.ws, request_actor(request))


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


def _apply_options(request: Request, ticket_id: str, offered: list[str], on: list[str]) -> None:
    """The addon ticket options the approve card showed (ticked = on). Best effort: the approval already happened, and a
    failing option write must not turn it into an error page."""
    if not offered:
        return
    from orch.addons import ticket_options
    try:
        ticket_options.apply_form(request.app.state.ws, ticket_id, offered, on, request_actor(request))
    except Exception:
        pass


@router.post("/t/{ref}/option")
def set_option(request: Request, ref: str, option: Annotated[str, Form()], value: Annotated[str, Form()] = "",
               next_url: Next = ""):
    """Turn an addon's ticket option (manifest `ticket_options`) on or off. Human only: this route is behind the dashboard
    token and the same-origin check, and `set_value` refuses any actor but a human."""
    from orch.addons import ticket_options
    ws = request.app.state.ws
    url = safe_next(next_url) or _ticket_url(request, ref)
    on = value in ("1", "on", "true")
    try:
        t_id = store.resolve(ws, ref).id
        addon, _, option_id = option.partition("/")
        label = next((o.label for la, o in ticket_options.declared(ws) if la.name == addon and o.id == option_id), option)
        ticket_options.set_value(ws, t_id, addon, option_id, on, request_actor(request))
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg=f"{label}: {'on' if on else 'off'}")


@router.post("/t/{ref}/approve")
def approve(request: Request, ref: str, gate: Annotated[str, Form()], seen: Annotated[str, Form()] = "",
            next_url: Next = "", despite_open_question: Annotated[str, Form()] = "",
            delegate: Annotated[str, Form()] = "", max_children: Annotated[str, Form()] = "",
            max_size: Annotated[str, Form()] = "", factory: Annotated[str, Form()] = "",
            option_offered: Annotated[list[str], Form()] = [], option_on: Annotated[list[str], Form()] = []):
    """`seen` is the hash of what the page showed (for an epic: its charter). `delegate` (epics, the checkbox in
    the confirm) opts in to delegation with `max_children` / `max_size`; Ops.approve checks the rest. `factory`
    (epics, Start as AI Factory) signs the factory charter with the factory's own limits (D5/D6) and wins over
    `delegate`; Ops refuses it while factory.enabled is off, and for any process under an agent harness."""
    if not seen:
        url = safe_next(next_url) or _ticket_url(request, ref)
        return back(url, err="reload the page and review again")
    despite = despite_open_question in ("1", "on", "true")
    if factory in ("1", "on", "true"):
        limits = {"factory": True}
    elif delegate in ("1", "on", "true"):
        limits = {"max_children": max_children.strip() or None, "max_size": max_size.strip() or None}
    else:
        limits = None
    def action():
        epic = _ops(request).approve(ref, gate, expected_hash=seen, despite_open_question=despite, delegate=limits)
        if limits and limits.get("factory"):
            _arm_runner(request.app.state.ws, epic, request_actor(request))  # only this dashboard Start lets the runner work for it
        _apply_options(request, epic.id, option_offered, option_on)

    return _run(request, ref, action, f"{gate} approved", next_url)


def _arm_runner(ws, epic, actor) -> None:
    from orch.core import epics, factory_sessions
    d = epics.delegation(ws, epic)
    if d and d.get("factory"):
        factory_sessions.arm(ws, actor, d["id"])


@router.post("/t/{ref}/approve-together")
def approve_together(request: Request, ref: str, seen: Annotated[str, Form()] = "",
                     seen_plan: Annotated[str, Form()] = "", next_url: Next = "",
                     despite_open_question: Annotated[str, Form()] = "",
                     option_offered: Annotated[list[str], Form()] = [], option_on: Annotated[list[str], Form()] = []):
    """F2: requirements and plan in one confirm. `seen` and `seen_plan` are the hashes of the two texts the page
    showed in full; Ops.approve_together checks each gate as a single approval would."""
    if not seen or not seen_plan:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    despite = despite_open_question in ("1", "on", "true")
    def action():
        t = _ops(request).approve_together(ref, requirements_hash=seen, plan_hash=seen_plan,
                                           despite_open_question=despite)
        _apply_options(request, getattr(t, "id", None) or store.resolve(request.app.state.ws, ref).id,
                       option_offered, option_on)

    return _run(request, ref, action, "requirements and plan approved", next_url)


@router.post("/t/{ref}/epic/pause")
def epic_pause(request: Request, ref: str, next_url: Next = ""):
    return _run(request, ref, lambda: _ops(request).epic_pause(ref), "delegation paused", next_url)


MAX_FEEDBACK_IMAGES = 8
_IMAGE_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def _image_ext(head: bytes) -> str | None:
    """The extension of an image by its first bytes (PNG, JPEG, GIF, WebP), never by the name or type the browser sent."""
    for magic, ext in _IMAGE_SIGNATURES:
        if head.startswith(magic):
            return ext
    return "webp" if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else None


def add_feedback_images(ws, ops: Ops, ref: str, files: list[UploadFile] | None, stem: str = "feedback") -> list[str]:
    """Store the images a person pasted or dropped into Send back as artifacts of kind `feedback` and return their names.
    Checked before anything is stored: at most MAX_FEEDBACK_IMAGES, each an image by its own bytes (the artifact size limit
    applies as for any artifact). The name is `<stem>-<n>.<ext>`, the next free one, so a second round never overwrites."""
    files = [f for f in files or [] if f.filename]  # an empty file input still sends one nameless part
    if not files:
        return []
    if len(files) > MAX_FEEDBACK_IMAGES:
        raise UsageError(f"at most {MAX_FEEDBACK_IMAGES} images can go with one message")
    tid = store.resolve(ws, ref).id
    kept = []
    for f in files:
        ext = _image_ext(f.file.read(16))
        f.file.seek(0)
        if ext is None:
            raise UsageError(f"{Path(f.filename).name or 'a file'} is not a PNG, JPEG, GIF or WebP image")
        kept.append((f, ext))
    names = []
    for f, ext in kept:
        n = 1  # the next number no earlier file of any type has taken, so a round reads sendback-1, -2, -3 ...
        while any((ws.artifacts_dir / tid / f"{stem}-{n}.{e}").exists() or f"{stem}-{n}.{e}" in names
                  for e in ("png", "jpg", "gif", "webp")):
            n += 1
        name = f"{stem}-{n}.{ext}"
        fd, tmp = tempfile.mkstemp(dir=ws.temporary_dir, prefix=".upload-")
        try:
            with open(fd, "wb") as out:
                shutil.copyfileobj(f.file, out)
            ops.artifact_add(ref, Path(tmp), name=name, kind="feedback", label="Your feedback")
        finally:
            Path(tmp).unlink(missing_ok=True)
        names.append(name)
    return names


def _acs(raw: list[str]) -> list[int]:
    """The criteria numbers a form named (`acs`, repeated or comma-separated); anything that is not a positive integer
    is dropped."""
    out = []
    for item in raw:
        for part in str(item).split(","):
            if part.strip().isdigit() and int(part) > 0:
                out.append(int(part))
    return out


def _with_images(request: Request, ref: str, files, action, success: str, next_url: str):
    """`action(names)` once the images are stored. A refusal after that (the page was out of date) leaves them linked
    as artifacts, which the error says."""
    url = safe_url_or_ticket(request, ref, next_url)
    ws, ops = request.app.state.ws, _ops(request)
    names: list[str] = []
    try:
        names = add_feedback_images(ws, ops, ref, files, stem="sendback")
        action(names)
    except OrchError as e:
        text = error_text(e)
        return back(url, err=text + (" The images stay on the ticket as artifacts." if names else ""))
    return back(url, msg=success)


def safe_url_or_ticket(request: Request, ref: str, next_url: str) -> str:
    return safe_next(next_url) or _ticket_url(request, ref)


@router.post("/t/{ref}/request-changes")
def request_changes(request: Request, ref: str, gate: Annotated[str, Form()], message: Annotated[str, Form()] = "",
                     seen: Annotated[str, Form()] = "", next_url: Next = "",
                     images: Annotated[Optional[list[UploadFile]], File()] = None):
    # Bound to the gate text the human read (like approve): a request about text that changed since is refused.
    if not seen:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    return _with_images(request, ref, images,
                        lambda names: _ops(request).request_changes(ref, gate, message, expected_hash=seen,
                                                                    attachments=names),
                        f"asked for changes on {gate}", next_url)


@router.post("/t/{ref}/answer")
def answer(request: Request, ref: str, qid: Annotated[str, Form()],
           value: Annotated[list[str], Form()] = [], note: Annotated[str, Form()] = "",  # FastAPI copies the default
           qhash: Annotated[str, Form()] = "", next_url: Next = "",
           images: Annotated[Optional[list[UploadFile]], File()] = None):
    # Bound to the question the human read (question_hash): an agent that re-asks it with other text or options
    # while the answer waits for its Undo window gets no answer to the new question.
    if not qhash:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    joined = ",".join(v for v in value if v.strip()) if len(value) > 1 else (value[0] if value else "")
    return _with_images(request, ref, images,
                        lambda names: _ops(request).answer(ref, qid, joined, note=note or None, expected_hash=qhash,
                                                            attachments=names),
                        f"answered {qid.upper()}", next_url)


@router.post("/t/{ref}/verdict")
def verdict(request: Request, ref: str, verdict: Annotated[str, Form()], message: Annotated[str, Form()] = "",
            next_url: Next = "", seen: Annotated[str, Form()] = "", acs: Annotated[list[str], Form()] = [],
            images: Annotated[Optional[list[UploadFile]], File()] = None):
    """`seen` (required): the hash of the criteria and evidence the page showed (orch.core.epics.verdict_hash; for
    an epic over its open children). Ops.verdict refuses it once they changed. A send-back may carry `acs` (the
    criteria it is about) and `images` (pasted or dropped screenshots, stored as feedback artifacts)."""
    if not seen:
        return back(safe_next(next_url) or _ticket_url(request, ref), err="reload the page and review again")
    if verdict != "follow-up":
        acs, images = [], None  # only feedback carries criteria and pictures
    return _with_images(request, ref, images,
                        lambda names: _ops(request).verdict(ref, verdict, message or None, expected_hash=seen,
                                                            acs=_acs(acs), attachments=names),
                        f"verdict {verdict}", next_url)


@router.post("/t/{ref}/move")
def move(request: Request, ref: str, to: Annotated[str, Form()], next_url: Next = ""):
    ops = _ops(request)
    url = safe_next(next_url) or _ticket_url(request, ref)
    try:
        ops.move(ref, to)
    except OrchError as e:
        return back(url, err=error_text(e))
    return back(url, msg="; ".join([f"moved to {to}", *ops.warnings]))


@router.post("/t/{ref}/close")
def close(request: Request, ref: str, as_: Annotated[str, Form(alias="as")] = "wont-do",
          by: Annotated[str, Form()] = "", message: Annotated[str, Form()] = "", next_url: Next = ""):
    """Close from any status, saying why (Ops.close: human only, a reason, --by for superseded and duplicate)."""
    by = by.strip() if as_ in SUCCEEDED else ""  # the field is hidden for the other choices but still posted
    return _run(request, ref, lambda: _ops(request).close(ref, message, as_, by or None),
                f"closed as {RESOLUTION_LABELS.get(as_, as_).lower()}", next_url)


@router.post("/t/{ref}/reopen")
def reopen(request: Request, ref: str, message: Annotated[str, Form()] = "", next_url: Next = ""):
    return _run(request, ref, lambda: _ops(request).reopen(ref, message), "reopened", next_url)


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
