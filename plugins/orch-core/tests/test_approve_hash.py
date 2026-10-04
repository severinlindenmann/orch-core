import pytest


def _ready(ws, put):
    return put("backlog", sections={"Requirements": "- r1", "Acceptance criteria": "- [ ] a1"})


def test_ops_approve_refuses_stale_hash(ws, put, hops):
    from orch.core import gates, store
    from orch.errors import ValidationError
    tid = _ready(ws, put)
    t = store.load(ws, tid)[1]
    seen = gates.gate_hash(t, "requirements")
    t.set_section("Requirements", "- r1\n- r2 added by an agent")
    store.save(ws, t)
    with pytest.raises(ValidationError, match="changed since you opened it"):
        hops.approve(tid, "requirements", expected_hash=seen)


def test_ops_approve_accepts_current_hash(ws, put, hops):
    from orch.core import gates, store
    tid = _ready(ws, put)
    seen = gates.gate_hash(store.load(ws, tid)[1], "requirements")
    hops.approve(tid, "requirements", expected_hash=seen)
    assert gates.gate_state(store.load(ws, tid)[1], "requirements") == "approved"


def test_dashboard_forms_send_seen(dash, ws, put):
    tid = _ready(ws, put)
    html = dash.get("/groom").text  # a backlog requirements approval: the one-by-one groom view (#11)
    assert f'action="/t/{tid}/approve"' in html and 'name="seen" value="sha256:' in html


def test_dashboard_approve_refuses_changed_text(dash, ws, put):
    from orch.core import gates, store
    tid = _ready(ws, put)
    seen = gates.gate_hash(store.load(ws, tid)[1], "requirements")
    t = store.load(ws, tid)[1]; t.set_section("Requirements", "- changed"); store.save(ws, t)
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen}, follow_redirects=False)
    assert r.status_code == 303 and "changed+since+you+opened+it" in r.headers["location"]
    assert gates.gate_state(store.load(ws, tid)[1], "requirements") != "approved"


def test_dashboard_approve_without_seen_is_refused(dash, ws, put):
    tid = _ready(ws, put)
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements"}, follow_redirects=False)
    assert "reload+the+page" in r.headers["location"]
