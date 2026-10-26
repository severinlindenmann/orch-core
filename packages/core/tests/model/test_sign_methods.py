"""B6 (owner decision F): the signed ``settings.sign_methods`` is enforced by the model, on append and on replay alike,
by the workspace state at the event's own position."""

import pytest

from orch.model import Code
from tests.model.world import World, refused


@pytest.fixture
def w():
    return World().bootstrap({"tom": "member"})


def methods(w, *allowed):
    w.wev("settings.changed", "sev", set={"sign_methods": list(allowed)})


def test_with_no_setting_every_way_is_allowed(w):
    uid = w.ticket()
    for auth in ("passphrase", "convenience", "secure-enclave-unlocked"):
        assert refused(w, uid, "log.added", "tom", text="x", auth=auth) is None, auth


def test_a_way_the_workspace_leaves_out_is_refused_and_the_others_are_not(w):
    uid = w.ticket()
    methods(w, "terminal", "phone")
    assert refused(w, uid, "log.added", "tom", text="x", auth="convenience") == Code.SIGN_METHOD
    assert refused(w, uid, "log.added", "tom", text="x", auth="secure-enclave-unlocked") is None
    assert refused(w, uid, "log.added", "tom", text="x", auth="passphrase") is None  # the terminal is always allowed
    methods(w, "terminal", "click")
    assert refused(w, uid, "log.added", "tom", text="x", auth="convenience") is None
    assert refused(w, uid, "log.added", "tom", text="x", auth="secure-enclave-unlocked") == Code.SIGN_METHOD
    methods(w, "terminal")
    assert refused(w, uid, "log.added", "tom", text="x", auth="convenience") == Code.SIGN_METHOD
    assert refused(w, uid, "log.added", "tom", text="x", auth="secure-enclave-unlocked") == Code.SIGN_METHOD


def test_the_setting_itself_is_judged_before_it_applies(w):
    """An owner who signed with a way the new setting excludes still changes the setting."""
    methods(w, "terminal", "phone")
    e = w.build_unappended(
        "workspace", "settings.changed", "sev", set={"sign_methods": ["terminal"]}, auth="secure-enclave-unlocked"
    )
    from orch.model import admit

    assert getattr(admit(w.state(), e, log="workspace"), "code", None) is None


def test_events_from_before_a_change_stay_valid_on_replay(w):
    """No retroactive invalidation: the log is judged position by position."""
    uid = w.ticket()
    w.tev(uid, "log.added", "tom", text="by click", auth="convenience")
    w.tev(uid, "log.added", "tom", text="by phone", auth="secure-enclave-unlocked")
    methods(w, "terminal")  # from here on neither is allowed
    state = w.state()
    assert not state._core.logs[uid].invalid and not state.workspace.invalid  # the earlier events are still valid
    assert refused(w, uid, "log.added", "tom", text="again", auth="convenience") == Code.SIGN_METHOD
    methods(w, "terminal", "click", "phone")  # allowed again: judged by the state at its own seq
    assert refused(w, uid, "log.added", "tom", text="again", auth="convenience") is None
    w.tev(uid, "log.added", "tom", text="by click again", auth="convenience")
    assert not w.state()._core.logs[uid].invalid


def test_an_event_appended_against_the_setting_is_invalid_on_replay(w):
    """A log written by a host that skipped the check: replay (the same function) marks the event invalid."""
    uid = w.ticket()
    methods(w, "terminal")
    w.tev(uid, "log.added", "tom", text="sneaked", auth="convenience")
    state = w.state()
    assert [x.code for x in state._core.logs[uid].invalid] == ["sign.method"]
