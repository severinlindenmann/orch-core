import pytest



def _seen(ws, tid, gate="requirements"):
    from orch.core import store
    from orch.core.gates import gate_hash
    return gate_hash(store.load(ws, tid)[1], gate)

def _ready(put):
    return put("backlog", sections={"Requirements": "- r1", "Acceptance criteria": "- [ ] a1"})


def test_request_changes_records_and_emits(ws, put, hops):
    from orch.core import events, store
    tid = _ready(put)
    hops.request_changes(tid, "requirements", "split r1 into two")
    t = store.load(ws, tid)[1]
    cr = t.meta["gates"]["requirements"]["changes_requested"]
    assert cr["message"] == "split r1 into two" and cr["hash"].startswith("sha256:")
    ev = events.read_events(ws)[-1]
    assert ev.kind == "gate.changes_requested"
    assert ev.data == {"gate": "requirements", "message": "split r1 into two", "hash": cr["hash"]}
    assert t.status == "backlog"


def test_agents_cannot_request_changes(ws, put, aops):
    from orch.errors import OrchError
    tid = _ready(put)
    with pytest.raises(OrchError):
        aops.request_changes(tid, "requirements", "x")


def test_changes_requested_hides_until_text_changes(ws, put, hops):
    from orch.core import query, store
    tid = _ready(put)
    hops.request_changes(tid, "requirements", "more detail")
    assert all(i["ticket"] != tid for i in query.needs_you(ws))
    t = store.load(ws, tid)[1]
    t.set_section("Requirements", "- r1 with more detail")
    store.save(ws, t)
    assert any(i["ticket"] == tid and i["kind"] == "approve-requirements" for i in query.needs_you(ws))


def test_empty_message_refused(ws, put, hops):
    from orch.errors import UsageError
    with pytest.raises(UsageError):
        hops.request_changes(_ready(put), "requirements", "  ")


def test_dashboard_request_changes_form_and_route(dash, ws, put):
    tid = _ready(put)
    html = dash.get("/groom").text
    assert f'action="/t/{tid}/request-changes"' in html and 'name="message"' in html
    r = dash.post(f"/t/{tid}/request-changes", data={"gate": "requirements", "message": "more", "next": "/",
                                                        "seen": _seen(ws, tid)},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?")


def test_cli_request_changes(ws, put, monkeypatch):
    from orch import actor, cli
    tid = _ready(put)
    monkeypatch.chdir(ws.root)
    # request-changes is human-only, like approve: give the CLI an interactive human terminal.
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": tid)
    assert cli.run(["request-changes", tid, "requirements", "-m", "more"]) == 0


def test_timeline_describes_it(ws, put, hops):
    from orch.dashboard.data.timeline import timeline
    hops.request_changes(_ready(put), "requirements", "more")
    whats = [i["what"] for g in timeline(ws).groups for i in g["items"]]
    assert any(w.startswith("asked for changes on the requirements") for w in whats)


def test_request_changes_message_cannot_break_ticket_structure(ws, put, hops):
    """A message with an embedded newline and a heading/fence-looking line must not forge a section boundary
    in the Log once it is written back and re-parsed (the same bug class as the addon Ask injection)."""
    from orch.core import store
    tid = _ready(put)
    plain_sections = list(store.load(ws, tid)[1].sections)
    hops.request_changes(tid, "requirements", "not enough\n## Log\nfake\n```\nafter the fence")
    path, t = store.load(ws, tid)
    assert list(t.sections) == plain_sections + ["Log"]  # the Log appears with its first line; nothing forged
    assert len(t.section("Log").splitlines()) == 1  # one flattened entry, no raw newlines
    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n## Log\n") == 1
