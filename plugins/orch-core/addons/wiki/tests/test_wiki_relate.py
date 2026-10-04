import pytest

from orch.core.model import new_ticket
from orch_wiki.relate import related_pages, search, stale_docs

PAGES = [
    {"provider": "github-wiki", "space": "A/b", "id": "Architecture", "title": "Architecture overview",
     "url": "https://github.com/A/b/wiki/Architecture", "documents": ["src/acme/ingest/**", "acme-energy-data:sql/*.sql"],
     "file_links": ["src/acme/ingest/loader.py"], "links": ["DEMO-0003"], "updated_at": "2026-10-02T13:10:00+00:00"},
    {"provider": "github-wiki", "space": "A/b", "id": "Runbook", "title": "Runbook: nightly load",
     "url": "https://github.com/A/b/wiki/Runbook", "documents": [], "file_links": ["src/acme/quality/checks.py"],
     "links": ["GH-12"], "updated_at": "2026-10-02T12:40:00+00:00"},
    {"provider": "github-wiki", "space": "A/b", "id": "Home", "title": "Home", "url": "https://github.com/A/b/wiki/Home",
     "updated_at": "2026-10-02T12:17:45+00:00"},
]
TEXTS = {"Architecture": "architecture overview\nthe ingest service loads readings. demo-3 moved the loader.",
         "Runbook": "runbook: nightly load\nlabel: ingest. ingest runs nightly. tracked in gh-12.",
         "Home": "home\nwelcome"}


def text_of(page):
    return TEXTS.get(page["id"], "")


def ticket(tid, **meta):
    t = new_ticket(tid, "T", type="feature", priority="normal", size="m", created="2026-10-02T09:00:00Z")
    t.meta.update(meta)
    return t


def test_stale_docs_by_glob_link_and_repo():
    diffs = [{"ticket": "DEMO-0003", "repo": "acme-energy-data", "files": ["src/acme/ingest/loader.py", "sql/tariffs.sql", "README.md"]},
             {"ticket": "DEMO-0004", "repo": "other", "files": ["sql/x.sql", "src/acme/quality/checks.py"]},
             {"ticket": "DEMO-0005", "repo": "acme-energy-data", "files": ["src/acme/ingest/x.py"]}]
    statuses = {"DEMO-0003": "testing", "DEMO-0004": "done", "DEMO-0005": "in-progress"}
    hints = stale_docs(diffs, PAGES, statuses)
    assert [(h.ticket, h.page_id) for h in hints] == [("DEMO-0003", "Architecture"), ("DEMO-0004", "Runbook")]
    assert hints[0].reason == "2 changed file(s) match: src/acme/ingest/loader.py, sql/tariffs.sql"
    assert hints[0].key == "DEMO-0003|A/b|Architecture"


def test_dismissed_hints_are_hidden():
    diffs = [{"ticket": "DEMO-0003", "repo": "r", "files": ["src/acme/ingest/loader.py"]}]
    assert stale_docs(diffs, PAGES, {"DEMO-0003": "done"}, {"DEMO-0003|A/b|Architecture"}) == []


def test_related_pages_ranked_by_mentions():
    t = ticket("DEMO-0003", labels=["ingest", "ux"], external=[{"key": "GH-12", "url": None}])
    assert related_pages(t, PAGES, text_of) == [(PAGES[1], "mentions GH-12, ingest ×2"), (PAGES[0], "mentions DEMO-3, ingest")]


def test_links_count_when_the_text_is_not_indexed():
    assert related_pages(ticket("DEMO-0003"), PAGES, lambda p: "") == [(PAGES[0], "mentions DEMO-0003")]


def test_no_mentions_no_pages():
    assert related_pages(ticket("DEMO-0099"), PAGES, text_of) == []


def test_search():
    assert [p["id"] for p in search(PAGES, text_of, "nightly")] == ["Runbook"]
    assert [p["id"] for p in search(PAGES, text_of, "LOADER ingest")] == ["Architecture"]
    assert search(PAGES, text_of, "   ") == []


def mentions_for(texts):
    from orch_wiki.pages import mentions
    return {p["id"]: mentions(str(p.get("title") or ""), texts.get(p["id"], "")) for p in PAGES}


def by_id(found):
    return lambda page: found.get(page["id"])


def no_scan(page):
    raise AssertionError("page text scanned during a render")


def test_precomputed_mentions_give_the_same_pages_without_a_text_scan():
    t = ticket("DEMO-0003", labels=["ingest", "ux"], external=[{"key": "GH-12", "url": None}])
    found = mentions_for(TEXTS)
    assert related_pages(t, PAGES, no_scan, by_id(found)) == related_pages(t, PAGES, text_of)


@pytest.mark.parametrize("text", [
    "demo-3a demo-3 x.demo-3, abc-demo-3 demo-3-4 demo-0003 demo-0003x xdemo-3",
    "ingest ingested re-ingest ingest_x ingest.py the-harness harness2 harness",
    "gh-12 gh-123 gh-12.5 (gh-12) agh-12",
])
def test_mentions_count_like_the_regex(text):
    t = ticket("DEMO-0003", labels=["ingest"], repos=["harness"], external=[{"key": "GH-12", "url": None}])
    texts = {"Home": text}
    page = [p for p in PAGES if p["id"] == "Home"]
    by_regex = related_pages(t, page, lambda p: texts.get(p["id"], ""))
    assert related_pages(t, page, no_scan, by_id(mentions_for(texts))) == by_regex and by_regex


def test_odd_terms_fall_back_to_a_capped_scan():
    """A label that is neither one word nor a key (data-model) is matched by regex, on at most MAX_SCANS pages."""
    from orch_wiki.pages import mentions
    from orch_wiki.relate import MAX_SCANS
    pages = [{"id": f"p{i}", "title": f"P{i}", "space": "A/b"} for i in range(MAX_SCANS * 3)]
    texts = {p["id"]: "see the data-model page" for p in pages}
    found = {p["id"]: mentions(p["title"], texts[p["id"]]) for p in pages}
    scans = []

    def text_of_counting(page):
        scans.append(page["id"])
        return texts[page["id"]]
    related = related_pages(ticket("DEMO-0001", labels=["data-model"]), pages, text_of_counting, by_id(found))
    assert related and related[0][1] == "mentions data-model" and len(scans) <= MAX_SCANS


def test_related_pages_at_the_caps_is_fast():
    """500 pages of 50 000 characters (ruling R12 caps): the render only looks counts up."""
    import time
    from orch_wiki.pages import MAX_INDEX_CHARS, mentions
    filler = ("the nightly ingest loads meter readings into bronze tables for acme energy; " * 700)[:MAX_INDEX_CHARS - 20]
    pages = [{"id": f"p{i}", "title": f"Page {i}", "space": "A/b"} for i in range(500)]
    found = {p["id"]: mentions(p["title"], filler + f" demo-{i}") for i, p in enumerate(pages)}  # fetch time, untimed
    t = ticket("DEMO-0003", labels=["ingest", "bronze", "acme"], repos=["harness"], external=[{"key": "GH-12", "url": None}])
    start = time.perf_counter()
    related = related_pages(t, pages, no_scan, by_id(found))
    took = time.perf_counter() - start
    assert related[0][0]["id"] == "p3" and took < 0.25, took  # target 50 ms; loose for slow CI machines
