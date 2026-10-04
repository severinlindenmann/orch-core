"""An open or backlog ticket nobody touched for `dashboard.revalidate_days` is flagged "idle", so it is re-checked
against the code before anyone builds it. Derived from `updated`, never stored; it blocks nobody."""
import json
from datetime import datetime, timezone

from orch.core.query import idle_days

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _old(ws, put, status, **meta):
    """A ticket last touched on 1 January (store.save always stamps `updated`, so the file is backdated by hand)."""
    import re
    from orch.core import store
    tid = put(status=status, **meta)
    path = store.resolve(ws, tid).path
    path.write_text(re.sub(r"(?m)^updated: .*$", "updated: 2026-01-01T09:00Z", path.read_text()))
    return tid


def test_an_old_open_ticket_is_flagged(ws):
    assert idle_days(ws, {"updated": "2026-08-01T09:00Z"}, "open", NOW) == 64


def test_a_recent_ticket_is_not(ws):
    assert idle_days(ws, {"updated": "2026-09-30T09:00Z"}, "backlog", NOW) is None


def test_exactly_the_limit_counts(ws):
    assert idle_days(ws, {"updated": "2026-09-04T12:00Z"}, "open", NOW) == 30


def test_only_backlog_and_open(ws):
    for status in ("in-progress", "waiting", "testing", "done"):
        assert idle_days(ws, {"updated": "2026-01-01T09:00Z"}, status, NOW) is None


def test_zero_turns_it_off(configure):
    ws = configure(dashboard={"revalidate_days": 0})
    assert idle_days(ws, {"updated": "2026-01-01T09:00Z"}, "open", NOW) is None


def test_the_limit_is_the_projects(configure):
    ws = configure(dashboard={"revalidate_days": 7})
    assert idle_days(ws, {"updated": "2026-09-26T12:00Z"}, "open", NOW) == 8


def test_a_bad_or_missing_stamp_is_not_flagged(ws):
    assert idle_days(ws, {"updated": "yesterday"}, "open", NOW) is None
    assert idle_days(ws, {}, "open", NOW) is None
    assert idle_days(ws, {"updated": 20261004}, "open", NOW) is None


def test_list_and_next_show_it(ws, put, capsys):
    from orch.cli import run
    tid = _old(ws, put, "open")
    fresh = put(status="open")
    assert run(["next"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith(tid))
    assert "idle" in line and "revalidate" in line
    assert "idle" not in next(x for x in out.splitlines() if x.startswith(fresh))
    assert run(["list", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[tid]["idle_days"] >= 30 and rows[fresh]["idle_days"] is None


def test_the_ticket_document_carries_it(ws, put):
    import jsonschema
    from orch.core import store
    from orch.core.schema import ticket_document, ticket_schema
    tid = _old(ws, put, "backlog")
    doc = ticket_document(ws, store.load(ws, tid)[1])
    assert doc["revalidate"]["idle_days"] >= 30
    jsonschema.validate(doc, ticket_schema())
    fresh = put(status="backlog")
    assert ticket_document(ws, store.load(ws, fresh)[1])["revalidate"] is None


def test_the_board_card_shows_it(ws, put, dash):
    tid = _old(ws, put, "open", title="Old idea")
    html = dash.get("/board").text
    assert "idle" in html and tid in html
