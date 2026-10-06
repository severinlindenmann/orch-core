from __future__ import annotations

import mimetypes
import os
import re

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from orch.core import evidence, query, store, tasks_view
from orch.core.epics import verdict_hash
from orch.core.events import read_events
from orch.core.gates import (GATE_SECTIONS, approved_snapshot, gate_hash, gate_state, invalidated_gates, plan_required,
                             requirements_skip_sizes)
from orch.core.protect import agent_wrote_ask, ask_author
from orch.clock import now as clock_now
from orch.core.lifecycle import allowed_targets
from orch.dashboard.data import artifact_view, story
from orch.dashboard.data import tasks as tasks_data
from orch.dashboard.data.agent_start import suggest as suggest_start
from orch.dashboard.data.agents import agent_rows
from orch.dashboard.data.cards import Cards
from orch.dashboard.data.steps import can_approve, day, meta_line, plan_checklist, steps, when, your_move
from orch.dashboard.reach import request_actor
from orch.dashboard.views import as_dict, as_list, page
from orch.widgets.render import css_names
from orch.errors import NotFoundError, TicketParseError, UsageError

router = APIRouter()

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
# Every artifact except PDF is served sandboxed: an agent-authored document (html, xhtml, xml, svg, ...)
# must not run scripts or submit forms with the dashboard's origin. Images and media still display.
SANDBOX_CSP = ("sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; media-src 'self'; "
               "form-action 'none'")


def load_or_error(request: Request, ref: str, entries: list[store.Entry] | None = None):
    """(ws, path, ticket, None) on success, or (ws, None, None, error_response). `entries` (a
    `store.scan`) may be shared with the rest of the request; it is scanned here when not given."""
    ws = request.app.state.ws
    try:
        entry = store.resolve(ws, ref, entries)
        path = entry.path
        ticket = store.read_ticket(path, path.relative_to(ws.home).as_posix())
        return ws, path, ticket, None
    except NotFoundError as e:
        return ws, None, None, page(request, "error.html", 404, nav="board", title="Not found",
                                    heading="Not found", message=e.message)
    except TicketParseError as e:
        entry = store.resolve(ws, ref, entries)
        raw = entry.path.read_text(encoding="utf-8", errors="replace")
        return ws, None, None, page(request, "error.html", 422, nav="board", title=f"{entry.id} cannot be read",
                                    heading=f"{entry.id} cannot be read",
                                    message=e.message, raw=raw)
    except UsageError as e:  # e.g. two files with the same id
        return ws, None, None, page(request, "error.html", 409, nav="board", title="Ambiguous ticket",
                                    heading="Ambiguous ticket", message=e.message)


def _drawn_html(ws, t) -> frozenset:
    """Names of the HTML artifacts an `html` block of the ticket really draws in a frame: the same test as
    `widgets.render.chrome` (agent HTML on, a renderer installed, block valid and pinned to the file as it is now,
    id not duplicated). A stale or wrong pin, or HTML off, leaves the file's own preview in place."""
    from orch.widgets import Ctx, frames, ticket_blocks, validate
    from orch.widgets.artifacts import name_of
    from orch.widgets.blocks import duplicate_ids
    ctx = Ctx.of(ws, t)
    if not (ctx.html and frames.INSTALLED):
        return frozenset()
    blocks = ticket_blocks(t)
    dup = duplicate_ids(blocks)
    return frozenset(n for b in blocks if b.layer == "html" and b.data.get("id") not in dup
                     and not validate(b, t, ws=ws) and (n := name_of(t.id, b.data.get("html"))))


def _artifacts(ws, ticket_id: str, drawn: frozenset = frozenset()) -> list[dict]:
    """The ticket's files; `drawn` marks those a widget block draws, which get no second (static) preview."""
    base = ws.artifacts_dir / ticket_id
    out = []
    for p in query.artifact_list(ws, ticket_id):
        name = p.relative_to(base).as_posix()
        ext = p.suffix.lower()
        kind = ("image" if ext in IMAGE_EXT else "html" if ext in (".html", ".htm")
                else "markdown" if ext == ".md" else "pdf" if ext == ".pdf" else "file")
        out.append({"name": name, "url": f"/a/{ticket_id}/{name}", "kind": kind, "size": p.stat().st_size,
                    "drawn": name in drawn})
    return out


def _safe_url(url) -> str | None:
    """Only plain web links become clickable; `javascript:` and other schemes stay text."""
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None


def _links(items) -> list[dict]:
    return [{**x, "url": _safe_url(x.get("url"))} for x in as_list(items) if isinstance(x, dict)]


_PR_NUMBER = re.compile(r"/(?:pull|pulls|pr|merge_requests)/(\d+)(?:[/?#]|$)")


def _repo_names(ws) -> dict[str, str]:
    """git.repos name -> the name a page shows ("Harness (name)" for the workspace's own repo)."""
    from orch.addons.api import workspace_repos
    try:
        return {r.name: r.label for r in workspace_repos(ws)}
    except Exception:
        return {}


def dedupe_prs(prs: list[dict], groups=()) -> list[dict]:
    """The Code panel lists a PR once: by URL among the ticket's own links, and not again when an addon's
    ticket.code table already shows it (the harness PR was listed under its repo and as a link)."""
    shown: set[str] = set()

    def walk(w):
        if isinstance(w, (list, tuple)):
            for x in w:
                walk(x)
            return
        url = getattr(w, "url", None)
        if isinstance(url, str) and url:
            shown.add(url)
        for attr in ("rows", "body"):
            walk(getattr(w, attr, ()) or ())

    for g in groups or ():
        walk(getattr(g, "widgets", ()))
    out, seen = [], set()
    for pr in prs:
        url = pr.get("url")
        if url and (url in seen or url in shown):
            continue
        if url:
            seen.add(url)
        out.append(pr)
    return out


def _pr_label(pr: dict, names: dict[str, str] | None = None) -> str:
    """"repo #N · state" for the Code panel. `orch link` writes "unknown" (before #165 "draft") as a placeholder that
    nothing refreshes, so both are left out; the live state comes from a code-review addon. A state set with
    `orch link --state` or by hand (open, merged, declined) still shows."""
    match = _PR_NUMBER.search(str(pr.get("url") or ""))
    repo = str(pr.get("repo") or "PR")
    name = (names or {}).get(repo, repo) + (f" #{match.group(1)}" if match else "")
    state = pr.get("state")
    return f"{name} · {state}" if isinstance(state, str) and state and state not in ("draft", "unknown") else name


def _question_view(q: dict) -> dict:
    rec = q.get("recommended")
    rec_keys = rec if isinstance(rec, list) else ([rec] if rec not in (None, "") else [])
    answer = q.get("answer")
    answered = answer not in (None, "")
    shown = ", ".join(answer) if isinstance(answer, list) else (str(answer) if answered else "")
    from orch.core.questions import question_hash
    # qhash: the question as shown; the answer form posts it and Ops.answer refuses a question that changed since
    return {**q, "rec_keys": [str(k) for k in rec_keys], "answered_flag": answered, "answer_text": shown,
            "qhash": question_hash(q)}


@router.get("/t/{ref}")
def ticket_page(request: Request, ref: str, open: str = "", show: str = "", act: str = ""):
    # One scan per request, shared by the load, needs_you, blockers, agent rows, the card and the menu badge.
    entries = store.scan(request.app.state.ws)
    ws, path, t, error = load_or_error(request, ref, entries)
    if error:
        return error
    skip = tuple(ws.config["gates"]["plan_skip_sizes"])
    req_skip = requirements_skip_sizes(ws)
    blockers = query.open_blockers(ws, t, entries)
    gate_seen = {g: gate_hash(t, g) for g in GATE_SECTIONS}
    from orch.core.gates import human_questions_in
    gate_question = {g: (human_questions_in(t, g) or [None])[0] for g in GATE_SECTIONS}
    from orch.core import ledger
    gate_unsigned = set(ledger.unsigned_gates(ws, t))
    task_view = tasks_view.view(ws, t, entries)
    working = t.status in ("in-progress", "waiting")
    tasks_card = (tasks_data.card(task_view, can_edit=working, plan_text=t.section("Plan"))
                  if task_view["tasks"] or task_view["error"] or working else None)
    all_needs = query.needs_you(ws, entries=entries)
    needs = [i for i in all_needs if str(i.get("ticket", "")).upper() == t.id.upper()]
    questions = sorted((_question_view(q) for q in t.meta.get("questions") or [] if isinstance(q, dict)),
                       key=lambda q: q["answered_flag"])
    claim = as_dict(t.meta.get("claim"))
    external = _links(t.meta.get("external"))
    names = _repo_names(ws)
    prs = [{**pr, "label": _pr_label(pr, names)} for pr in _links(t.meta.get("prs"))]
    branches = {names.get(str(k), k): v for k, v in as_dict(t.meta.get("branches")).items()}
    # allow_override: a gate whose text still reads as a question for the human can be approved with the explicit
    # "not a question for me" checkbox (the server checks it, Ops.approve)
    can = {g: can_approve(t, g, plan_skip_sizes=skip, allow_override=True) for g in GATE_SECTIONS}
    actions = {
        # never offer an Approve that Ops.approve would refuse (or that a pending change request blocks)
        "approve_requirements": can["requirements"],
        "approve_plan": can["plan"],
        "verdict": t.status == "testing",
        "accept": t.status == "testing" and not invalidated_gates(t),  # a changed gate is re-approved first
        "moves": allowed_targets(t, request_actor(request), plan_skip_sizes=skip, open_blockers=blockers,
                                 requirements_skip_sizes=req_skip),
        "release": bool(claim.get("session")),
        "close": t.status != "done" and t.meta.get("type") != "epic" and request_actor(request).is_human,
        "reopen": t.status == "done" and request_actor(request).is_human,
    }
    # F2: requirements and plan drafted together: one confirm approves both (each bound to its own hash)
    together = next((i for i in needs if i.get("kind") == "approve-requirements" and i.get("together")), None) \
        if can["requirements"] and t.meta.get("type") != "epic" else None
    gates = {g: gate_state(t, g) for g in GATE_SECTIONS}
    # A PR opened while the plan still needs approval is worth a warning (spec §4.4).
    pr_early = bool(t.meta.get("prs")) and plan_required(ws, t) and gates["plan"] != "approved"
    ticket_events = read_events(ws, t.id)
    at = clock_now()
    runtime = getattr(request.app.state, "addons", None)
    reviews = runtime.review_index() if runtime is not None else {}
    failing = [i["url"] for i in reviews.get(t.id, []) if i.get("state", "open") == "open"
               and isinstance(i.get("checks"), dict) and i["checks"].get("state") == "failed" and isinstance(i.get("url"), str)]
    ran_on = _ran_on(t)
    ticket_decisions = runtime.decisions_for(t.id) if runtime is not None else []
    rows = agent_rows(ws, now=at, events=ticket_events, entries=entries, needs=all_needs)
    card = Cards(ws, entries=entries, needs=all_needs, rows=rows, events=ticket_events, reviews=reviews,
                 mentions=runtime.mention_index() if runtime is not None else None,
                 now=at).for_ticket(t, tasks=None if task_view["error"] else _task_items(t))
    start_box = suggest_start(ws, t, needs_items=needs, now=at, events=ticket_events, rows=rows, failing_prs=failing,
                             request=request)
    move = your_move(t, needs, plan_skip_sizes=skip, moves=actions["moves"], requirements_skip_sizes=req_skip)
    chapter = story.current_chapter(t, card)
    gate_views = {g: story.gate_view(ws, t, g, card, can_approve=can[g], seen=gate_seen[g],
                                     snapshot=approved_snapshot(ws, t.id, g), question=gate_question[g],
                                     unsigned=g in gate_unsigned) for g in GATE_SECTIONS}
    stale = bool(card["agent"]) and card["agent"]["status"] == "stale"
    epic_view = None
    if t.meta.get("type") == "epic":
        from orch.dashboard.data.epic import page_data
        all_events = read_events(ws)  # the delegation's audit spans the children's events
        epic_view = page_data(ws, t, entries=entries, needs=all_needs, events=all_events, show=show,
                              gate=t.status in ("backlog", "open") and bool(actions["approve_requirements"]),
                              asks=sum(1 for q in questions if not q["answered_flag"]),
                              move_human=move.get("kind") == "human",
                              builder=Cards(ws, entries=entries, needs=all_needs, events=all_events, now=at,
                                            reviews=reviews))
    # The status card answers blocking questions in place; other open questions are answered in Agreed.
    card_qids = {q["id"] for q in questions if not q["answered_flag"] and q.get("blocking", True)} \
        if (move.get("action") or {}).get("kind") == "answer" else set()
    step_list = steps(t, plan_skip_sizes=skip, requirements_skip_sizes=req_skip)
    ask_by = ask_author(t, ticket_events)
    widgets, ac_chips = _widgets(ws, path, t)
    return page(request, "ticket.html", nav="board", title=f"{t.id} {t.title}", needs=all_needs,
                widgets=widgets, ac_chips=ac_chips, widget_css=css_names(),
                waiting=query.waiting(ws, entries=entries, needs=all_needs, now=at), t=t, meta=t.meta,
                path=path.relative_to(ws.home).as_posix(), card=card, chapter=chapter, open_all=open == "all", act=act,
                chapter_titles=story.CHAPTER_TITLES, chapters=story.CHAPTERS,
                steps=step_list, journey=story.journey(t, card, step_list, ticket_events, ask_by,
                                                        story.gate_signers(ws, t),
                                                        story.done_signer(ws, t, ticket_events)), move=move, stale=stale,
                meta_line=meta_line(t, needs, at, events=ticket_events), created_day=day(t.meta.get("created")),
                gate_views=gate_views, pr_early=pr_early, questions=questions, card_qids=card_qids, actions=actions,
                verdict_seen=verdict_hash([t], ws) if actions["verdict"] else "", invalidated=invalidated_gates(t),
                criteria=evidence.criteria(t), other_evidence=evidence.other_evidence(t),
                evidence_by=story.evidence_author(ticket_events),
                ask_by=ask_by, ask_agent_editable=agent_wrote_ask(ws, t, ticket_events),
                notes=story.agent_notes(t, ticket_events), timeline=story.timeline(ticket_events),
                plan_checklist=plan_checklist(t.section("Plan")) if not task_view["tasks"] else None,
                artifacts=_artifacts(ws, t.id, _drawn_html(ws, t)), artifact_view=artifact_view.view(ws, t), blockers=blockers, claim=claim, claim_at=when(claim.get("at")), ran_on=ran_on,
                external=external, prs=prs, branches=branches, start_box=start_box, tasks_card=tasks_card,
                ticket_decisions=ticket_decisions, epic=epic_view, together=bool(together),
                together_questions=human_questions_in(t, "requirements") + human_questions_in(t, "plan"))


def _ran_on(t, limit: int = 5) -> list[dict]:
    """The ticket's recorded sessions, newest first, with the models Claude Code's own transcript says ran."""
    from orch.dashboard import agentinfo
    from orch.dashboard.data.agent_start import HARNESS_LABELS
    rows = [s for s in t.meta.get("sessions") or [] if isinstance(s, dict) and s.get("id")]
    rows.sort(key=lambda s: str(s.get("started") or ""), reverse=True)
    out = []
    for s in rows[:limit]:
        harness = str(s.get("harness") or "")
        claude = "claude" in harness.lower()
        # sessions record the actor name (claude-code, copilot-cli); the labels are keyed by launcher (claude, copilot)
        label = HARNESS_LABELS.get(harness.removesuffix("-code").removesuffix("-cli"), harness)
        out.append({"harness": label or "agent", "started": day(s.get("started")), "claude": claude,
                    "models": agentinfo.session_models(s["id"]) if claude else None})
    return out


def _widgets(ws, path, t):
    """The ticket's ```orch blocks for `section_md` (parsed from the file as read, so errors name its lines) and the
    verdict chip per criterion projected from the `checks` blocks in Verification."""
    from markupsafe import Markup

    from orch.dashboard.markdown import section_widgets
    from orch.widgets.types.checks import projection, verdict_chip
    chips = {n: Markup(verdict_chip(v["verdict"])) for n, v in projection(t).items()}
    return section_widgets(ws, t, path.read_text(encoding="utf-8", errors="replace")), chips


def _task_items(t):
    from orch.core import tasks as tk
    return tk.ticket_tasks(t)


@router.get("/t/{ref}/raw")
def raw_page(request: Request, ref: str):
    ws = request.app.state.ws
    try:
        entry = store.resolve(ws, ref)
    except UsageError as e:
        return page(request, "error.html", 404, nav="board", title="Not found", heading="Not found", message=e.message)
    return page(request, "raw.html", nav="board", title=f"{entry.id} raw file", tid=entry.id, path=entry.path.relative_to(ws.home).as_posix(),
                text=entry.path.read_text(encoding="utf-8", errors="replace"),
                editable=entry.meta is not None)  # the editor needs a file that parses


@router.get("/a/{ticket}/{name:path}")
def artifact(request: Request, ticket: str, name: str, v: str = ""):
    ws = request.app.state.ws
    target = query.artifact_file(ws, ticket, name)
    if target is None:
        return PlainTextResponse("not found", status_code=404)
    root = query.artifact_root(ws, ticket)
    target = root / name  # as named, unresolved: a link anywhere along it is refused by the open
    ext = target.suffix.lower()
    media = "text/plain; charset=utf-8" if ext == ".md" else (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    headers = {"X-Content-Type-Options": "nosniff"}
    if ext != ".pdf":  # the browser's PDF viewer needs to run; everything else is locked down
        headers["Content-Security-Policy"] = SANDBOX_CSP
    if v:  # an inline link names the content it expects (?v=<sha256 prefix>): a file swapped since is not served
        from orch.core.artifacts import read_pinned
        data = read_pinned(target, v, root=root)  # read once, hashed as read, and exactly those bytes are sent
        if data is None:
            return PlainTextResponse("this file changed since it was linked in the ticket; it is not shown",
                                     status_code=409, headers={"Cache-Control": "no-store"})
        from orch.dashboard.ranges import ranged
        return ranged(request, data, media, headers)
    from orch.core.artifacts import open_regular
    fd = open_regular(target, root)  # the bytes sent come from this one handle, streamed
    if fd is None:
        return PlainTextResponse("not found", status_code=404)

    f = os.fdopen(fd, "rb")  # owned from here on: closed by the generator, or when the response is dropped unread
    size = os.fstat(f.fileno()).st_size

    def chunks():
        left = size  # never more than the length announced, even if the file grows meanwhile
        with f:
            while left > 0 and (block := f.read(min(1 << 16, left))):
                left -= len(block)
                yield block

    headers["Content-Length"] = str(size)
    return StreamingResponse(chunks(), media_type=media, headers=headers, background=BackgroundTask(f.close))
