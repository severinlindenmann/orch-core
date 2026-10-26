"""The Wiki HTTP surface: four GET routes with closed queries."""

from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict

from .. import reads
from ..errors import fail
from . import create, index, reader, scope, search


class EmptyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageQuery(EmptyQuery):
    slug: str


class SearchQuery(EmptyQuery):
    q: str


MESSAGES = {
    "ok": None,
    "no_repo": "This workspace has no Git repository.",
    "no_commits": "This repository has no commits yet.",
    "no_folder": "No wiki folder in this project yet.",
}


def linked(rows, links):
    for row in rows:
        row["tickets"] = [r["ticket"] for r in links.get(row["slug"], [])]
        row["links"] = len(row["tickets"])
    return rows


def register(app, gateway):
    @app.get("/api/w/{ws}/wiki")
    def listing(query: Annotated[EmptyQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = scope.open_wiki(gateway, ws)
        result = (
            reader.list_pages(sc.repo)
            if sc.repo is not None
            else {"state": "no_repo", "pages": [], "total": 0, "cut": False}
        )
        result.pop("sizes", None)
        if result["pages"]:
            linked(result["pages"], index.links_by_slug(sc.link, ws, sc.person))
        return {"available": True, "message": MESSAGES[result["state"]], **result}

    @app.post("/api/w/{ws}/wiki/create")
    def create_folder(body: EmptyQuery, ws: str = Depends(reads.workspace)):
        return create.create(gateway, ws)

    @app.get("/api/w/{ws}/wiki/page")
    def page(query: Annotated[PageQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = scope.open_wiki(gateway, ws)
        reader.validate(query.slug)
        if sc.repo is None:
            raise fail("not_found", "That page does not exist.", exit=2)
        result = reader.get_page(sc.repo, query.slug)
        backlinks = index.links_by_slug(sc.link, ws, sc.person).get(query.slug, [])
        return {**result, "backlinks": backlinks, "tickets": [r["ticket"] for r in backlinks]}

    @app.get("/api/w/{ws}/wiki/search")
    def searching(query: Annotated[SearchQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = scope.open_wiki(gateway, ws)
        result = search.search(sc, query.q)
        if result["pages"]:
            linked(result["pages"], index.links_by_slug(sc.link, ws, sc.person))
        return result

    @app.get("/api/w/{ws}/wiki/backlinks")
    def backlinks(query: Annotated[PageQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = scope.open_wiki(gateway, ws)
        slug = reader.validate(query.slug)
        return {"slug": slug, "tickets": index.links_by_slug(sc.link, ws, sc.person).get(slug, [])}
