from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile

from orch.core.constants import PRIORITIES, SIZES, TYPES
from orch.core.ops import Ops
from orch.dashboard.routes_actions import add_uploads
from orch.dashboard.views import HUMAN, back, error_text, page
from orch.errors import OrchError

router = APIRouter()


DONE_WHEN = "Everything in Requirements is done."
MODES = ("ticket", "factory", "dark")
NAMES = {"factory": "an AI Factory", "dark": "a Dark AI Factory"}


class FormOnce:
    """One-time tokens for the New ticket form's factory starts, so a double submit never starts two runs. Kept in
    memory (bounded, oldest dropped first): a token is issued with each rendered form and taken by the first POST that
    carries it; the epic that POST created is remembered for a second one."""
    LIMIT = 512

    def __init__(self):
        self._lock = threading.Lock()
        self._open: OrderedDict[str, bool] = OrderedDict()
        self._used: OrderedDict[str, str | None] = OrderedDict()

    @staticmethod
    def _trim(d: OrderedDict) -> None:
        while len(d) > FormOnce.LIMIT:
            d.popitem(last=False)

    def issue(self) -> str:
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._open[token] = True
            self._trim(self._open)
        return token

    def take(self, token: str) -> tuple[bool, str | None]:
        """(True, None) for a token issued and not used yet; else (False, the epic its first use created or None)."""
        with self._lock:
            if token and self._open.pop(token, None):
                self._used[token] = None
                self._trim(self._used)
                return True, None
            return False, self._used.get(token)

    def created(self, token: str, epic_id: str) -> None:
        with self._lock:
            if token in self._used:
                self._used[token] = epic_id


def _once(request: Request) -> FormOnce:
    return request.app.state.new_once


def _plain(text: str) -> str:
    """Flash text without Markdown code marks."""
    return text.replace("`", "")


def _form(request: Request, values: dict, problem: str | None = None, status_code: int = 200,
          problem_href: str | None = None):
    from orch.core import dark_profile, epics, factory_runner, permits
    ws = request.app.state.ws
    git = ws.config.get("git") or {}
    dark_on = permits.dark_on(ws)
    from orch.dashboard.data.factory import release_offers
    return page(request, "new.html", status_code, nav="new", title="New ticket", types=TYPES, sizes=SIZES, priorities=PRIORITIES,
                values=values, problem=problem, problem_href=problem_href, review_term=git.get("review_term") or "PR",
                factory_on=permits.enabled(ws), dark_on=dark_on, done_when_default=DONE_WHEN,
                profile_empty=dark_on and not dark_profile.rules(ws),
                edits_off=permits.enabled(ws) and factory_runner.edits_why(),
                limits=epics.FACTORY_DEFAULTS, once=_once(request).issue(), offers=release_offers(ws, dark_on))


def release_problem(ws, release: str, rollback: bool, confirm_production: str) -> str | None:
    """Why a Dark start cannot sign this "Release up to" choice, or None: the same checks on New ticket and on the
    epic page, made here whatever the form offered (Ops checks the recipe again before it signs)."""
    if release not in ("", "none", "merge", "dev", "prod"):
        return "Release up to is nothing, merge, dev or production"
    if rollback and release != "prod":
        return "Rolling back by itself goes only with Release up to Production"
    if release == "prod" and confirm_production.strip() != "production":
        return "Type production to let the runner release to production by itself"
    if release in ("merge", "dev", "prod"):
        from orch.core import factory_release
        from orch.core.epics import FACTORY_DEFAULTS  # a dashboard start signs the factory's default time budget
        why = factory_release.release_blocker(ws, release, rollback=rollback, max_hours=FACTORY_DEFAULTS["max_hours"])
        if why:
            return f"No release can be signed ({_plain(why)})"
    return None


def _mode_problem(ws, mode: str, title: str, ask: str, done_when: str, confirm: str, release: str = "",
                  rollback: bool = False, confirm_production: str = "", close: bool = False) -> str | None:
    """Why this mode cannot start, judged here from the switches (never from what the form offered) and from the text
    the human typed, before anything is created, or None. The text checks are the ones the start itself makes, so a
    start refused for them leaves no epic behind."""
    from orch.core import permits
    from orch.core.gates import _HUMAN_QUESTION
    from orch.textsafe import decodes_to_hidden
    if mode not in MODES:
        return "Unknown mode: nothing was created."
    if mode != "dark" and close:
        return "Only a Dark AI Factory closes the epic by itself: nothing was created."
    if mode != "dark" and rollback:
        return "Only a Dark AI Factory signs a release and its rollback: nothing was created."
    if mode == "ticket":
        return None
    if not permits.enabled(ws):
        return permits.off_reason(ws) + ": nothing was created."
    if mode == "dark":
        if not permits.dark_on(ws):
            return ("Dark AI Factory is off in this checkout: nothing was created. Turn it on in a terminal with "
                    "orch factory dark on.")
        if confirm.strip() != "dark":
            return "Type dark to start a Dark AI Factory: nothing was created."
        why = release_problem(ws, release, rollback, confirm_production)
        if why:
            return f"{why}: nothing was created."
    elif release not in ("", "none") or rollback:
        return "Only a Dark AI Factory signs a release: nothing was created."
    if close and mode != "dark":
        return "Only a Dark AI Factory closes the epic by itself: nothing was created."
    if not ask.strip():
        return "Describe the work in Ask: it becomes the epic's Requirements. Nothing was created."
    if not done_when.strip():
        return "Say when it is done: it becomes the epic's Acceptance criteria. Nothing was created."
    if any(decodes_to_hidden(x) for x in (title, ask, done_when)):
        return ("Your text holds hidden or control characters (for example a zero-width space from a copy and paste). "
                "Remove them and send it again: nothing was created.")
    asks = [line.strip() for text in (ask, done_when) for line in text.split("\n") if _HUMAN_QUESTION.search(line)]
    if asks:
        return (f"This line reads as a question still open for you: \"{asks[0][:80]}\". An epic does not start while "
                "its text asks something: decide it in the text, or leave the line out. Nothing was created.")
    return None


def _escape_alt(name: str) -> str:
    """Escape markdown-significant characters in an image's alt text (the filename)."""
    return name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


@router.get("/new")
def new_form(request: Request):
    return _form(request, {"type": "feature", "size": "m", "priority": "normal", "mode": "ticket", "done_when": DONE_WHEN})


@router.post("/new")
def create(
    request: Request,
    title: Annotated[str, Form()],
    type_: Annotated[str, Form(alias="type")] = "feature",
    size: Annotated[str, Form()] = "m",
    priority: Annotated[str, Form()] = "normal",
    external: Annotated[str, Form()] = "",
    ask: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "ticket",
    done_when: Annotated[str, Form()] = "",
    confirm_dark: Annotated[str, Form()] = "",
    release: Annotated[str, Form()] = "",
    rollback: Annotated[str, Form()] = "",
    confirm_production: Annotated[str, Form()] = "",
    close: Annotated[str, Form()] = "",
    once: Annotated[str, Form()] = "",
    files: Annotated[Optional[list[UploadFile]], File()] = None,
):
    """`mode` ticket (as before), factory or dark: an epic whose Requirements are the ask as typed and whose Acceptance
    criteria are `done_when`, started at once as an AI Factory (or a Dark one, with `confirm_dark` = "dark") through the
    epic page's own start, which arms the runner. The switches are checked here, whatever the form offered, and `once`
    (the form's one-time token) makes a second submit of the same form start nothing."""
    ws = request.app.state.ws
    ops = Ops(ws, HUMAN)
    ask = ask.replace("\r\n", "\n").strip()
    done_when = done_when.replace("\r\n", "\n").strip()
    factory = mode in ("factory", "dark")
    values = {"title": title, "type": "epic" if factory else type_, "size": size, "priority": priority,
              "external": external, "ask": ask, "mode": mode, "done_when": done_when or DONE_WHEN, "release": release}
    release = release if mode == "dark" else ""  # the field shows only in Dark mode; another mode signs no release
    # a rollback or a close posted outside Dark mode is refused, never silently dropped (_mode_problem)
    roll = rollback in ("1", "on", "true")
    shut = close in ("1", "on", "true")  # the auto-close: shown only in Dark mode
    problem = _mode_problem(ws, mode, title, ask, done_when, confirm_dark, release, roll, confirm_production, shut)
    if problem:
        return _form(request, values, problem, 422)
    if factory:
        fresh, earlier = _once(request).take(once)
        if not fresh:  # a second submit of the same form, or one this dashboard did not render: start nothing
            blank = {"type": "feature", "size": "m", "priority": "normal", "mode": "ticket", "done_when": DONE_WHEN}
            if earlier:
                return _form(request, blank, f"This form was sent already: it created {earlier}. Nothing more was "
                                             "created.", 409, problem_href=f"/factory/{earlier}")
            return _form(request, blank, "This form was sent already or is too old: nothing was created. Look on the "
                                         "Factories page before you start it again.", 409, problem_href="/factory")
    try:
        if factory:  # the ask is the Requirements, word for word; nothing else is written for the human
            t = ops.new(title, type="epic", size=size, priority=priority, external=external.strip() or None,
                        sections={"Requirements": ask, "Acceptance criteria": done_when})
        else:
            # The same split as `orch new --body-file` (#24): gated headings become their sections, other `##`
            # headings stay in the Ask one level down, a heading naming another orch section or an open fence is
            # refused.
            from orch.core.body import split_body
            ask, sections = split_body(ask)
            t = ops.new(title, type=type_, size=size, priority=priority, ask=ask, external=external.strip() or None,
                        sections=sections)
    except OrchError as e:
        return _form(request, values, _plain(error_text(e)), 422)
    if factory:
        _once(request).created(once, t.id)
    # The ticket now exists: any failure past this point must not be reported as a form
    # validation error (422), which would invite a duplicate ticket on resubmit.
    try:
        added = add_uploads(ws, ops, t.id, files or [])
        if added:
            # Angle-bracket destination (CommonMark) so names with spaces/parens work in
            # Obsidian and in the dashboard; alt text escaped since it is plain markdown.
            refs = "\n".join(f"![{_escape_alt(p.name)}](<../../artifacts/{t.id}/{p.name}>)" for p in added)
            ops.set_section(t.id, "Ask", refs if factory else f"{ask}\n\n{refs}".strip())
    except OrchError as e:
        return back(f"/t/{t.id}", err=_plain(f"created {t.id}, but attaching files failed: {error_text(e)}"))
    if not factory:
        return back(f"/t/{t.id}", msg=f"created {t.id} in backlog")
    # The start binds the charter of the epic exactly as created here (`t`, no children yet): a change made to it in
    # between makes the approval refuse, and the human starts it from the epic page instead.
    from orch.core import epics, store
    from orch.dashboard.routes_actions import start_factory
    name = NAMES[mode]
    stored = store.load(ws, t.id)[1]
    if stored.section("Requirements") != ask or stored.section("Acceptance criteria") != done_when:
        return back(f"/t/{t.id}", err=f"created {t.id}, but its Requirements or Acceptance criteria are not exactly what "
                                      f"you typed, so it was not started as {name}. Read it, then start it here.")
    try:
        start_factory(ws, t.id, epics.charter(ws, t, tickets=[])["content_hash"],
                      {"factory": True, **({"dark": True} if mode == "dark" else {}),
                       **({"release": release} if release in ("merge", "dev", "prod") else {}),
                       **({"rollback": True} if roll else {}), **({"close": True} if shut else {})})
    except OrchError as e:
        return back(f"/t/{t.id}", err=_plain(f"created {t.id}, but starting it as {name} failed: {error_text(e)}"))
    return back(f"/factory/{t.id}", msg=f"created {t.id} and started it as {name}")
