"""Addon pages and the human POSTs behind addon widgets (spec v2 §11.5, §11.7, A1 §5.5, §5.7). Addons register no
routes: everything here is core. Every POST needs an Origin header equal to the host (`strict_same_origin`)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.background import BackgroundTask

from orch.addons import intents
from orch.addons.api import FileResult, Reveal
from orch.addons.loader import _log_error, valid_name
from orch.addons.runtime import clean_params
from orch.addons.widgets import MAX_TARGET_LEN
from orch.core.events import append_event
from orch.dashboard.addon_files import save_upload, stage_download
from orch.dashboard.auth import strict_same_origin
from orch.dashboard import remote_gate
from orch.dashboard.reach import SCOPE_KEY, request_actor
from orch.dashboard.views import back, confirm_page, error_text, page, safe_next
from orch.errors import OrchError

router = APIRouter()


def _refused() -> PlainTextResponse:
    return PlainTextResponse("cross-origin request refused", status_code=403)


def _return_to(request: Request, fallback: str) -> str:
    """The same-origin page the form was posted from, else `fallback`."""
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc and referer.netloc == request.headers.get("host", "") and referer.path.startswith("/") \
            and not referer.path.startswith("//"):
        return referer.path + (f"?{referer.query}" if referer.query else "")
    return fallback


@router.get("/addons/{name}")
def addon_page_slash(name: str):
    if not valid_name(name):
        return PlainTextResponse("not found", status_code=404)
    return RedirectResponse(f"/addons/{name}/", status_code=303)


@router.get("/addons/{name}/")
def addon_page(request: Request, name: str):
    runtime = request.app.state.addons
    group = runtime.page(name, params=clean_params(request.query_params)) if valid_name(name) else None
    if group is None:
        return page(request, "error.html", 404, nav="", title="Not found", heading="Not found",
                    message=f"No addon page {name!r} is enabled for this workspace.")
    la = runtime.registry.get(name)
    return page(request, "addon_page.html", nav=f"addon:/addons/{name}/", title=group.title, group=group,
                can_refresh=la is not None and la.has("provider"))


@router.post("/addons/{name}/refresh")
def refresh(request: Request, name: str):
    """Queue a refresh for the scheduler and return at once: no addon code runs in this request."""
    if not strict_same_origin(request):
        return _refused()
    if request.app.state.addons.registry.get(name) is None:
        return back("/", err=f"addon {name!r} is not enabled here")
    queued = request.app.state.scheduler.request_refresh(name)
    return back(_return_to(request, f"/addons/{name}/"), msg="Refresh started" if queued else "Nothing to refresh")


@router.post("/addons/{name}/actions/{action_id}")
def run_action(request: Request, name: str, action_id: str, target: str = Form(""),
               file: UploadFile | None = File(None), ask: str = Form(""), return_to: str = Form("")):
    # A sync route: FastAPI runs it in a worker thread, so a slow act() never blocks the event loop.
    # A multipart body was already size-checked by upload_limit_middleware (app.py) before it was parsed.
    if not strict_same_origin(request):
        return _refused()
    if remote_gate.action_unlisted(request, name, action_id):  # before anything else; the middleware says it first
        return remote_gate.refusal_response()
    ws = request.app.state.ws
    # return_to: the page a no-JS confirm page was opened from (its own URL is this POST's referer)
    dest = safe_next(return_to) or _return_to(request, f"/addons/{name}/")
    la = request.app.state.addons.registry.get(name)
    spec = la.manifest.action(action_id) if la else None
    if spec is None or not callable(getattr(la.obj, "act", None)):
        return back(dest, err=f"unknown action {action_id!r}")
    if len(target) > MAX_TARGET_LEN:
        return back(dest, err="that action target is too long")
    if ask and not spec.accepts_file:  # posted without JS from the dialog-tier form: confirm on a page first
        return confirm_page(request, action=f"/addons/{name}/actions/{action_id}", fields=[("target", target), ("return_to", dest)],
                            title=spec.confirm or f"{spec.label}?", body=f"The {la.manifest.title} addon carries this out and may send data off this machine.",
                            confirm=spec.label, cancel_href=dest, nav=f"addon:/addons/{name}/")
    refusal = remote_gate.action_target_refusal(request, ws, target)
    if refusal:
        return back(dest, err=refusal)
    upload, kwargs = None, {}
    try:
        try:
            if spec.accepts_file:
                if file is None or not file.filename:
                    return back(dest, err="choose a file first")
                limit, types = spec.accepts_file
                if SCOPE_KEY in request.scope:
                    limit = min(limit, remote_gate.REMOTE_ADDON_UPLOAD)
                upload = save_upload(ws, name, file, limit, types)
                kwargs["upload"] = upload
            # never an Ops: an Intent (or a FileResult / Reveal) comes back
            result = la.obj.act(action_id, target, la.ctx.provider_context(), **kwargs)
        except OrchError as e:
            return back(dest, err=error_text(e))
        except Exception:
            # the target only, never the result or the traceback's locals: a Reveal's text must not reach a log
            _log_error(ws, name, f"action {action_id} {target[:200]}")
            return back(dest, err=f"{spec.label} failed (see recent addon errors on Workspace & addons)")
    finally:
        if upload is not None:
            upload.path.unlink(missing_ok=True)  # the addon had its chance; nothing stays in in/
    extra: dict = {}
    if isinstance(result, FileResult):
        try:
            token = request.app.state.downloads.put(stage_download(ws, name, result))
        except OrchError as e:
            return back(dest, err=error_text(e))
        out = RedirectResponse(f"/addons/{name}/files/{token}", status_code=303)
        extra["result"] = "file"
    elif isinstance(result, Reveal):
        token = request.app.state.reveals.put({"label": str(result.label)[:200], "text": str(result.text)[:4000]})
        out = back(dest, msg=f"{spec.label}: done")
        out.headers["location"] += f"&reveal={token}"  # back() always adds msg=, so there is a query already
        extra["result"] = "reveal"
    else:
        try:
            intent = intents.as_intent(result)
            refusal = remote_gate.action_refusal(request, ws, intent)
            if refusal:
                return back(dest, err=refusal)
            message = intents.execute(ws, intent, allowed_ref=target, tickets=spec.tickets, actor=request_actor(request), source="act")
        except OrchError as e:
            return back(dest, err=error_text(e))
        if intent.kind != "none":
            extra["intent"] = intent.kind
        out = back(dest, msg=str(message or f"{spec.label}: done")[:300])
    append_event(ws, None, "addon.action", request_actor(request), {"addon": name, "action": action_id, "target": target[:200], **extra})
    request.app.state.scheduler.request_refresh(name)
    return out


_FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "no-store"}


@router.get("/addons/{name}/files/{token}")
def download(request: Request, name: str, token: str):
    """A file an action handed back, served once. The token is used up by any try, also one with another addon's
    name, so a guessed or leaked URL never gets a second go; the file is deleted after it was sent."""
    item = request.app.state.downloads.pop(token)
    path = Path(item["path"]) if item is not None else None
    if item is None or item["addon"] != name or path.is_symlink() or not path.is_file():
        if path is not None:
            path.unlink(missing_ok=True)
        return PlainTextResponse("not found", status_code=404, headers=_FILE_HEADERS)
    return FileResponse(path, media_type=item["mime"] or "application/octet-stream", filename=item["name"],
                        content_disposition_type="attachment", headers=_FILE_HEADERS,
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/addons/{name}/decisions")
def resolve(request: Request, name: str, id: str = Form(...), choice: str = Form(...)):
    # The decision id travels in a form field, never in the path, so any id the addon picks is safe.
    if not strict_same_origin(request):
        return _refused()
    ws = request.app.state.ws
    dest = _return_to(request, "/")
    found = request.app.state.addons.find_decision(name, id)
    if found is None:
        return back(dest, err="that item is gone; reload the page")
    la, decision = found
    if choice not in {value for value, _ in decision.choices}:
        return back(dest, err=f"{choice!r} is not a choice for this item")
    if decision.stale and choice == "apply":
        return back(dest, err="this item is stale, so it was not applied")
    if not callable(getattr(la.obj, "resolve", None)):
        return back(dest, err="this addon cannot resolve items")
    try:
        result = la.obj.resolve(id, choice, la.ctx.provider_context())  # never an Ops: an Intent comes back
    except OrchError as e:
        return back(dest, err=error_text(e))
    except Exception:
        _log_error(ws, name, f"resolve {id[:200]} {choice[:50]}")
        return back(dest, err="could not apply it (see recent addon errors on Workspace & addons)")
    try:
        intent = intents.as_intent(result)
    except OrchError as e:
        return back(dest, err=error_text(e))
    refusal = remote_gate.decision_refusal(request, ws, intent)
    if refusal:
        return back(dest, err=refusal)
    try:
        # decisions=: defence in depth; find_decision only returns items of addons with the decisions capability
        message = intents.execute(ws, intent, allowed_ref=decision.ticket, tickets=False, actor=request_actor(request),
                                  source="resolve", decisions=la.has("decisions"), anchor=decision.anchor)
        outcome, text = "applied", str(message or "Done")[:300]
    except OrchError as e:
        outcome, text = "refused", error_text(e)[:300]
    if intent.kind != "none":
        hook = getattr(la.obj, "on_intent_result", None)
        if callable(hook):
            try:
                hook(id, outcome, text)
            except Exception:
                _log_error(ws, name, f"on_intent_result {id[:200]}")
    append_event(ws, None, "addon.decision", request_actor(request), {"addon": name, "decision": id[:200], "choice": choice[:50],
                                                      "intent": intent.kind, "ref": decision.ticket,
                                                      "outcome": outcome,
                                                      **({"origin": decision.origin} if decision.origin else {})})
    return back(dest, msg=text) if outcome == "applied" else back(dest, err=text)
