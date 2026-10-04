import pytest

pytest.importorskip("fastapi")


def test_workspace_shows_doctor_checks_and_branding(dash):
    html = dash.get("/workspace").text
    for text in ("Setup checks", "config", "Repositories", "Branding", "Housekeeping", "Configuration"):
        assert text in html


def test_copy_fix_button_carries_command(dash, ws, monkeypatch):
    from orch import onboarding
    monkeypatch.setattr(onboarding, "doctor", lambda start=None, **kw: [onboarding.Check("hooks", False, "no commit check in x", "orch hooks install")])
    html = dash.get("/workspace").text
    assert 'data-copy="orch hooks install"' in html


def test_setup_badge_never_breaks_pages(dash, monkeypatch):
    from orch import onboarding
    monkeypatch.setattr(onboarding, "open_setup_items", lambda ws: 1 / 0)
    assert dash.get("/").status_code == 200


def test_setup_count_is_cached_and_expires(dash, monkeypatch):
    """Without the serve loop (no lifespan here) a page recomputes an expired badge count itself."""
    from orch import onboarding
    from orch.dashboard import setup_state

    calls = {"n": 0}

    def fake_doctor(start=None, **kw):
        calls["n"] += 1
        return [onboarding.Check("hooks", False, "no commit check", "orch hooks install")]

    monkeypatch.setattr(onboarding, "doctor", fake_doctor)
    setup_state._STATES.clear()  # drop whatever the dash fixture's own warm-up GET computed
    clock = {"t": 1000.0}
    monkeypatch.setattr(setup_state.time, "monotonic", lambda: clock["t"])

    dash.get("/")
    dash.get("/")
    assert calls["n"] == 1

    clock["t"] += setup_state.TTL + 1
    dash.get("/")
    assert calls["n"] == 2


def test_setup_checks_run_in_the_background_with_lifespan(ws, monkeypatch):
    """`orch serve`: doctor, hook states and orch check run in the lifespan loop; pages never start git."""
    import subprocess
    import time as _time
    from fastapi.testclient import TestClient
    from orch import onboarding
    from orch.dashboard import setup_state
    from orch.dashboard.app import create_app

    calls = {"n": 0}
    real = onboarding.doctor

    def counting(start=None, **kw):
        calls["n"] += 1
        return real(start, **kw)

    monkeypatch.setattr(onboarding, "doctor", counting)
    monkeypatch.setattr(setup_state, "RECHECK_AFTER", 1e9)  # no visit-triggered round while we count
    with TestClient(create_app(ws, "tok")) as c:
        st = setup_state.state(ws)
        deadline = _time.monotonic() + 30
        while st.snapshot is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert st.snapshot is not None and st.background

        def refuse(*a, **k):
            raise AssertionError("subprocess during a page render")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        before = calls["n"]
        for url in ("/?token=tok", "/", "/board", "/workspace", "/workspace"):
            assert c.get(url).status_code == 200, url
        assert calls["n"] == before
        assert "Setup checks" in c.get("/workspace").text


def test_workspace_post_makes_the_next_visit_fresh(ws, monkeypatch):
    import time as _time
    from fastapi.testclient import TestClient
    from orch import onboarding
    from orch.dashboard import setup_state
    from orch.dashboard.app import create_app

    monkeypatch.setattr(setup_state, "RECHECK_AFTER", 1e9)  # only the POST may cause a new round
    with TestClient(create_app(ws, "tok")) as c:
        c.get("/?token=tok")
        st = setup_state.state(ws)
        deadline = _time.monotonic() + 30
        while st.snapshot is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        monkeypatch.setattr(onboarding, "doctor", lambda start=None, **kw: [
            onboarding.Check("hooks", False, "fresh-marker-after-post", "orch hooks install")])
        assert "fresh-marker-after-post" not in c.get("/workspace").text  # the loop's result, not recomputed
        c.post("/workspace/tidy", headers={"origin": "http://testserver"}, follow_redirects=False)
        assert "fresh-marker-after-post" in c.get("/workspace").text


def test_workspace_shows_error_when_doctor_fails(dash, monkeypatch):
    from orch import onboarding

    def boom(start=None, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding, "doctor", boom)
    r = dash.get("/workspace")
    assert r.status_code == 200
    assert "could not run the setup checks" in r.text


def test_badge_never_waits_for_the_first_background_round(ws):
    from orch.dashboard import setup_state
    from orch.dashboard.views import _setup_count
    st = setup_state.state(ws)
    st.background = True  # as while the lifespan loop's first round is still running
    try:
        assert _setup_count(ws) == 0 and st.snapshot is None
    finally:
        st.background = False



def test_a_reader_waits_for_the_round_started_after_invalidate(ws, monkeypatch):
    import threading

    from orch import onboarding
    from orch.dashboard import setup_state

    st = setup_state.SetupState(ws)
    marks = iter(["before", "after"])
    started, release = threading.Event(), threading.Event()

    def doctor(start=None, **kw):
        mark = next(marks)
        if mark == "after":
            started.set()
            release.wait(5)
        return [onboarding.Check("hooks", False, mark, None)]

    monkeypatch.setattr(onboarding, "doctor", doctor)
    st.compute()  # the loop's earlier round
    st.invalidate()  # a POST changed something
    loop_round = threading.Thread(target=st.compute)  # the woken loop starts its round first ...
    loop_round.start()
    started.wait(5)
    got = []
    reader = threading.Thread(target=lambda: got.append(st.get()))  # ... and the next page reads meanwhile
    reader.start()
    reader.join(0.2)
    assert reader.is_alive()  # it waits for the running round instead of showing the old result
    release.set()
    loop_round.join(5)
    reader.join(5)
    assert got[0].checks[0].message == "after"
    assert not st.stale
