"""Add-on events in follow-ups (#718): the shape, the match and the signed checks. T0:
``uv run pytest tests/model/test_followup_addon_events.py -x -q``"""

from datetime import timedelta

import pytest

from orch.model import Code
from orch.model import followup_events as fe
from tests.model.test_followups_engine import _fire_with  # noqa: PLC2701
from tests.model.world import World, digest, refused, stamp

DECL = {
    "kind": "run.finished",
    "title": "Run finished",
    "statuses": ["succeeded", "failed"],
    "ref": "int",
    "interval_s": 300,
}
DBX = "databricks.run.finished"


def grant(w, name, events, **kw):
    w.wev(
        "addon.granted", "sev", name=name, version="1.0.0", package_sha256=digest(name), capabilities=["network"],
        fields={}, sections=[], artifact_kinds=[], events=events, **kw,
    )  # fmt: skip


@pytest.fixture
def env():
    w = World().bootstrap({"tom": "member"})
    w.wev(
        "addon.granted", "sev", name="schedules", version="1.0.0", package_sha256=digest("schedules"),
        capabilities=[], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip
    grant(w, "databricks", [DECL])
    grant(w, "other", [DECL])
    uid = w.ticket(owner="tom")
    return w, uid, w.claim(uid, "tom")


def set_on(w, uid, a, on):
    return w.try_(
        uid, "followup.set", a, followup="fu_" + w.nid(), trigger="addon", on=on,
        deadline=stamp(w.clock + timedelta(hours=3)), brief="x",
    )  # fmt: skip


def wake(kind=DBX, ref="8812", status="succeeded", at="2026-01-01T00:00:00Z"):
    return fe.addon_event(kind, ref, status, at)


def addons(w):
    return w.state()._core.ws.addons


def test_parse_on_addon_forms_and_refusals(env):
    w, *_ = env
    ad = addons(w)
    assert fe.parse_on(DBX + ":8812", "w", ad) == {"source": "addon", "kind": DBX, "ref": "8812"}
    assert fe.parse_on(DBX + ":8812", "w", ad, only="failed")["status"] == "failed"
    for bad, only in ((DBX + ":abc", None), (DBX + ":1", "weird"), ("databricks.nope.done:1", None), (DBX, None)):
        with pytest.raises(ValueError):
            fe.parse_on(bad, "w", ad, only)
    with pytest.raises(ValueError):
        fe.parse_on("question.answered", "w", ad, "failed")  # a status filter is for add-on events only
    assert fe.parse_on("question.answered", "w", ad)["source"] == "orch"


def test_match_takes_any_declared_status_unless_filtered():
    m = {"source": "addon", "kind": DBX, "ref": "8812"}
    assert fe.matches(m, wake(status="failed")) and fe.matches(m, wake(status="succeeded"))
    assert not fe.matches({**m, "status": "succeeded"}, wake(status="failed"))
    assert not fe.matches(m, wake(ref="1")) and not fe.matches(m, wake(kind="other.run.finished"))
    assert not fe.valid_match({**m, "extra": 1}) and not fe.valid_match({**m, "ref": ""})
    assert not fe.valid_shape({**wake(), "ref": ""}) and not fe.valid_shape({**wake(), "extra": 1})


def test_wake_line_names_the_addon_and_the_ref():
    line = fe.wake_line("addon", None, wake())
    assert "databricks" in line and "8812" in line and "run.finished.succeeded" in line


def test_set_checks_the_signed_declaration(env):
    w, uid, a = env
    ok = {"source": "addon", "kind": DBX, "ref": "8812"}
    _, result = set_on(w, uid, a, ok)
    assert not hasattr(result, "code"), result
    for bad in (
        {**ok, "ref": "abc"},  # not an int
        {**ok, "kind": "databricks.other.thing"},  # undeclared
        {**ok, "kind": "ghost.run.finished"},  # unknown add-on
        {**ok, "status": "weird"},
        {"source": "orch", "kind": "question", "status": "answered"},
    ):
        _, result = set_on(w, uid, a, bad)
        assert result.code == Code.FOLLOWUP_DENIED, bad
    _, result = w.try_(
        uid, "followup.set", a, followup="fu_" + w.nid(), trigger="addon", on=ok, hook="0" * 64,
        deadline=stamp(w.clock + timedelta(hours=3)), brief="x",
    )  # fmt: skip
    assert result.code == Code.FOLLOWUP_DENIED  # one trigger per follow-up: no webhook beside an add-on event


def test_fired_needs_the_matching_declared_event_of_that_addon_only(env):
    w, uid, a = env
    e, _ = set_on(w, uid, a, {"source": "addon", "kind": DBX, "ref": "8812"})
    w.clock += timedelta(minutes=5)
    at = stamp(w.clock)
    # add-on `other` reporting done for the same ref is not databricks' event
    _, r = _fire_with(w, uid, e, "addon", event=wake(kind="other.run.finished", at=at))
    assert r.code == Code.FOLLOWUP_DENIED
    _, r = _fire_with(w, uid, e, "addon", event=wake(status="weird", at=at))
    assert r.code == Code.FOLLOWUP_DENIED
    _, r = _fire_with(w, uid, e, "addon", event=wake(ref="1", at=at))
    assert r.code == Code.FOLLOWUP_DENIED
    _, r = _fire_with(w, uid, e, "addon")  # an add-on wake carries its event
    assert r.code == Code.FOLLOWUP_DENIED
    _, r = _fire_with(w, uid, e, "addon", event=wake(status="failed", at=at))
    assert not hasattr(r, "code"), r
    v = w.view(uid).followups[0]
    assert v["state"] == "fired" and v["event"]["status"] == "failed"


def test_turning_the_addon_off_cancels_a_waiting_follow_up(env):
    w, uid, a = env
    set_on(w, uid, a, {"source": "addon", "kind": DBX, "ref": "8812"})
    assert w.view(uid).followups[0]["state"] == "pending"
    w.wev("addon.disabled", "sev", name="databricks")
    v = w.view(uid).followups[0]
    assert v["state"] == "cancelled" and v["reason"] == "addon_disabled"


def test_a_new_grant_that_drops_the_event_cancels_it(env):
    w, uid, a = env
    set_on(w, uid, a, {"source": "addon", "kind": DBX, "ref": "8812"})
    grant(w, "databricks", [])
    assert w.view(uid).followups[0]["state"] == "cancelled"


def test_deadline_fallback_is_addon_silent(env):
    w, uid, a = env
    e, _ = set_on(w, uid, a, {"source": "addon", "kind": DBX, "ref": "8812"})
    w.clock += timedelta(hours=3)
    _, r = _fire_with(w, uid, e, "deadline", fallback="addon_silent")
    assert not hasattr(r, "code"), r


def test_the_cli_parses_an_addon_event_with_an_optional_status_filter(env):
    w, *_ = env
    ad = addons(w)
    assert fe.parse_on(DBX + ":8812", w.state()._core.ws.workspace_id, ad)["ref"] == "8812"
    assert fe.parse_on(DBX + ":8812", "w", ad, only="succeeded")["status"] == "succeeded"


@pytest.mark.parametrize(
    "events",
    [
        [DECL, dict(DECL)],  # the same kind twice
        [{**DECL, "title": "Run \u202efinished"}],  # a bidi control in the title
        [{**DECL, "title": "Run\nfinished"}],  # a newline
        [{**DECL, "title": " Run finished "}],  # padded
    ],
)
def test_a_replayed_grant_refuses_duplicate_kinds_and_titles_that_are_not_plain_text(env, events):
    w, *_ = env
    code = refused(
        w, "workspace", "addon.granted", "sev", name="newone", version="1.0.0", package_sha256=digest("newone"),
        capabilities=["network"], fields={}, sections=[], artifact_kinds=[], events=events,
    )  # fmt: skip
    assert code == Code.ADDON_VALUE_INVALID
    assert (
        refused(
            w, "workspace", "addon.granted", "sev", name="okay", version="1.0.0", package_sha256=digest("okay"),
            capabilities=["network"], fields={}, sections=[], artifact_kinds=[], events=[DECL],
        )
        is None
    )  # fmt: skip
