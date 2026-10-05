"""wiki: related pages, docs that may need an update and search (spec A1 §7.4); the local folder provider and
Create page from ticket write only new files under the wiki folder, and commit them (the owner's one exception to
"orch never commits")."""
from __future__ import annotations

from pathlib import Path

from orch.addons.api import Intent
from orch.errors import ValidationError

from . import render
from .confluence import ConfluencePages
from .create import create_page
from .diffs import BranchDiffs
from .dismissed import Dismissed
from .github_wiki import GitHubWikiPages
from .local import BodyReader, LocalPages, resolve_folder
from .pages import IndexReader
from .relate import TARGET, stale_docs


class WikiAddon:
    def __init__(self, ctx):
        self.ctx = ctx
        self.page = f"page.{ctx.name}"
        self.providers = [GitHubWikiPages(), ConfluencePages(), LocalPages(), BranchDiffs()]
        self.index = IndexReader(ctx.state_dir)
        self.bodies = BodyReader(ctx.state_dir)
        self.dismissed = Dismissed(ctx.state_dir)
        self._memo = (None, None)  # (view, (statuses, dismissed keys)): one tickets scan and one file read per view

    def text_of(self, page: dict) -> str:
        texts = self.index.texts(str(page.get("provider") or ""), str(page.get("space") or ""))
        return texts.get(str(page.get("id") or ""), "")

    def raw_text_of(self, page: dict) -> str:
        raws = self.index.raw(str(page.get("provider") or ""), str(page.get("space") or ""))
        return raws.get(str(page.get("id") or ""), "")

    def body_of(self, page: dict) -> str | None:
        return self.bodies.bodies(str(page.get("space") or "")).get(str(page.get("id") or ""))

    def page_source(self, page_id: str):
        """(the page text, the wiki folder) of a local page, for core's frame of a widget on it: the text is the cached
        body the page was drawn from, so a digest in a frame address is found in the very text that drew it."""
        settings = self.ctx.settings
        if settings.get("provider") != "local":
            return None
        folder, _ = resolve_folder(self.ctx.root, settings)
        if folder is None or not isinstance(page_id, str):
            return None
        space = folder.relative_to(Path(self.ctx.root)).as_posix()
        body = self.body_of({"space": space, "id": page_id})
        return (body, space) if body is not None else None

    def mentions_of(self, page: dict) -> dict | None:
        found = self.index.mentions(str(page.get("provider") or ""), str(page.get("space") or ""))
        return found.get(str(page.get("id") or ""))

    def _per_view(self, view) -> tuple[dict, set]:
        """Ticket statuses and dismissed keys, read once per view. A new view (the next render) reads again, so a
        dismiss or a status change shows at once. The memo is one tuple, swapped whole, so threads never mix."""
        memo = self._memo
        if memo[0] is view:
            return memo[1]
        data = ({entry.id: entry.status for entry in self.ctx.tickets()}, self.dismissed.keys())
        self._memo = (view, data)
        return data

    def statuses(self, view) -> dict:
        return self._per_view(view)[0]

    def hints(self, view) -> list:
        statuses, dismissed = self._per_view(view)
        return stale_docs(render.diff_items(view), render.pages_of(view), statuses, dismissed)

    def widgets(self, slot, view):
        if slot == self.page:
            return render.page(view, self)
        if slot == "ticket.pages":
            return render.ticket_panel(view, self)
        return []

    def decisions(self, view):
        return render.decisions(view, self)

    def resolve(self, decision_id, choice, ctx):
        """Dismiss only stores the hint key; it never answers or approves anything (A1: a none intent)."""
        key = decision_id.removeprefix("docs|") if isinstance(decision_id, str) and decision_id.startswith("docs|") else ""
        if choice != "dismiss" or not TARGET.fullmatch(key):
            raise ValidationError("unknown item or choice; reload the page")
        self.dismissed.add(key)
        return Intent("none", reason=f"Hidden on {key.split('|', 1)[0]}.")

    def act(self, action_id, target, ctx):
        if action_id == "create_page":
            return Intent("none", reason=create_page(ctx, target))
        if action_id != "dismiss" or not TARGET.fullmatch(target or ""):
            raise ValidationError("that hint reference is not valid; reload the page")
        Dismissed(ctx.addon.state_dir).add(target)
        return Intent("none", reason=f"Hidden on {target.split('|', 1)[0]}.")


def create(ctx):
    return WikiAddon(ctx)
