"""Named reads for declarative add-on views (design D7, ``addon.view``).

A declarative node (``tree``, ``search``) never carries a URL. It names a read, ``<addon>.<name>``, and core answers it
from this registry. Each read is an explicit contract: a name, a closed args model, the limits it enforces and a result
shape the dashboard's zod schema checks again. Reads are GET-only and answer from the workspace's committed files
(HEAD of the wiki folder) with the same path-safety helpers as the rest of the wiki; they run no add-on code and need
no capability. The add-on being ON, the person being a member and the repository being confirmed are checked on every
call by ``wiki.scope.open_wiki``.

Result shapes (what the dashboard validates):

* tree    ``{state, message, nodes:[{id, label, hint?, badge?, children?}], total, cut, offer?}``
          ``children``: true when a further read with ``parent=<id>`` lists them (lazy). ``offer``: one plain action
          the viewer may take when there is nothing to show (``{label, action}``).
* page    ``{id, title, markdown|null, updated|null, by|null}``; ``markdown`` null means too large to show.
* search  ``{results:[{id, label, snippet}], total, cut}``
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import reads
from .errors import fail
from .wiki import index, limits, reader, scope, search

NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}")

MESSAGES = {
    "no_repo": "This workspace has no Git repository.",
    "no_commits": "This repository has no commits yet.",
    "no_folder": "No wiki folder in this project yet.",
}


class TreeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent: Annotated[str, Field(min_length=1, max_length=limits.SLUG_MAX)] | None = None


class PageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(min_length=1, max_length=limits.SLUG_MAX)]


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Annotated[str, Field(min_length=1, max_length=limits.MAX_QUERY)]


@dataclass(frozen=True)
class Read:
    """One named read: who owns it, what it takes, what it never exceeds, and how it answers."""

    name: str
    args: type[BaseModel]
    limits: dict[str, int]
    run: Callable


def _links(sc, ws, rows):
    by_slug = index.links_by_slug(sc.link, ws, sc.person)
    for row in rows:
        row["links"] = len(by_slug.get(row["slug"], []))
    return rows


def wiki_tree(gateway, ws: str, args: TreeArgs) -> dict:
    sc = scope.open_wiki(gateway, ws)
    if args.parent is not None:
        # The wiki folder is flat: no page has children.
        return {"state": "ok", "message": None, "nodes": [], "total": 0, "cut": False}
    raw = (
        reader.list_pages(sc.repo)
        if sc.repo is not None
        else {"state": "no_repo", "pages": [], "total": 0, "cut": False}
    )
    pages = raw["pages"]
    if pages:
        _links(sc, ws, pages)
    offer = None
    if raw["state"] == "no_folder" and sc.role in ("owner", "maintainer"):
        offer = {"label": "Create the wiki folder", "action": "create"}
    nodes = []
    for row in pages[: limits.LIST_MAX]:
        node = {"id": row["slug"], "label": row["title"], "hint": row["slug"]}
        if row["links"]:
            node["badge"] = f"{row['links']} linked {'ticket' if row['links'] == 1 else 'tickets'}"
        nodes.append(node)
    return {
        "state": raw["state"],
        "message": MESSAGES.get(raw["state"]),
        "nodes": nodes,
        "total": raw["total"],
        "cut": raw["cut"],
        **({"offer": offer} if offer else {}),
    }


def wiki_page(gateway, ws: str, args: PageArgs) -> dict:
    sc = scope.open_wiki(gateway, ws)
    reader.validate(args.id)
    if sc.repo is None:
        raise fail("not_found", "That page does not exist.", exit=2)
    page = reader.get_page(sc.repo, args.id)
    return {key: page[key] for key in ("title", "markdown", "updated", "by")} | {"id": args.id}


def wiki_search(gateway, ws: str, args: SearchArgs) -> dict:
    sc = scope.open_wiki(gateway, ws)
    found = search.search(sc, args.q)
    results = [{"id": r["slug"], "label": r["title"], "snippet": r["snippet"]} for r in found["pages"]]
    return {"results": results[: limits.SEARCH_HITS], "total": len(results), "cut": found["cut"]}


READS: dict[str, Read] = {
    r.name: r
    for r in (
        Read("wiki.tree", TreeArgs, {"nodes": limits.LIST_MAX}, wiki_tree),
        Read("wiki.page", PageArgs, {"bytes": limits.PAGE_MAX_BYTES}, wiki_page),
        Read("wiki.search", SearchArgs, {"results": limits.SEARCH_HITS, "query": limits.MAX_QUERY}, wiki_search),
    )
}


def _view_file() -> bytes:
    return (resources.files("orch_dashboard") / "wiki" / "view.json").read_bytes()


def packages() -> list[dict]:
    """The add-on packages whose screens are declarative views served by this dashboard (today: wiki). The view file is
    a copy of ``packages/addons/wiki/views/wiki.json`` (a test pins them byte for byte)."""
    raw = _view_file()
    doc = json.loads(raw)
    return [
        {
            "name": doc["addon"],
            "title": "Wiki",
            "version": "0.1.0",
            "description": "Team notes: markdown pages in the project's wiki folder.",
            "capabilities": [],
            "first_party": True,
            "package_sha256": hashlib.sha256(raw).hexdigest(),
            "update": None,
            "contributions": doc["contributions"],
        }
    ]


def register(app, gateway):
    @app.get("/api/addons")
    def addon_packages():
        return packages()

    @app.get("/api/w/{ws}/view/{name}")
    def view(name: str, request: Request, ws: str = Depends(reads.workspace)):
        read = READS.get(name) if NAME.fullmatch(name) else None
        if read is None:
            raise fail("not_found", "That read does not exist.", exit=2)
        try:
            args = read.args.model_validate(dict(request.query_params))
        except ValidationError:
            raise fail("invalid.input", "Those arguments are not valid for this read.", exit=5) from None
        return read.run(gateway, ws, args)
