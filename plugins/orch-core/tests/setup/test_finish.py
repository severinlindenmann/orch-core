"""B9 step 6: facts and Finish setup (R7: never resurrects), inside store.locked()."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace as NS

import pytest

from orch.host import signer as host_signer
from orch.setup import facts as fx
from orch.setup.facts import Facts, catalog, missing_items, read_facts
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, make_env
from .fakes import PERSON, WID, FakePorts, FakeTerminal, names
from .test_create import GRANT_SECRET, progress  # noqa: F401  (autouse fixture below)

INFO = {a.id: a for a in catalog()}
ME = PERSON


class FakeStore:
    def __init__(self, ws) -> None:
        self.state = NS(workspace=ws)
        self.locked_calls = 0
        self.inside = False

    @contextlib.contextmanager
    def locked(self):
        self.locked_calls += 1
        self.inside = True
        try:
            yield
        finally:
            self.inside = False


def ws(**kw):
    base = dict(
        workspace_id=WID,
        prefix="ANN",
        members={ME: NS(name="Ann")},
        former={},
        addons={},
        settings={"grant_hours": 8},
        grants={},
    )
    base.update(kw)
    return NS(**base)


def facts_of(w, **kw):
    return read_facts(FakeStore(w), ME, **{"runner": {}, "deposit_confirmed": False, "convenience": False, **kw})


def test_facts_are_read_inside_the_store_lock():
    class Checking(FakeStore):
        @property
        def state(self):
            assert self.inside, "the state was read outside store.locked()"
            return self._state

        @state.setter
        def state(self, value):
            self._state = value

    s = Checking(ws())
    f = read_facts(s, ME, runner={}, deposit_confirmed=False, convenience=False)
    assert f.workspace_id == WID and s.locked_calls == 1


def test_unreadable_state_fails_closed():
    class Broken(FakeStore):
        @property
        def state(self):
            raise OSError("corrupt")

        @state.setter
        def state(self, v):
            pass

    with pytest.raises(SetupError) as e:
        read_facts(Broken(ws()), ME, runner={}, deposit_confirmed=False, convenience=False)
    assert e.value.code == "setup.facts_unreadable"


PROF = {**PROFILE, "addons": ["dashboard", "terminals", "worktrees"], "agent_grant": True, "agent_hours": 24}


def test_r7_nothing_done_everything_missing():
    f = facts_of(ws())
    assert missing_items(f, PROF, INFO) == [
        "addon:dashboard",
        "addon:terminals",
        "addon:worktrees",
        "runner",
        "settings",
        "runner_grant",
    ]


@pytest.mark.parametrize("state", ["granted", "revoked", "removed", "purged", "disabled"])
def test_r7_an_addon_with_any_history_is_never_granted_again(state):
    a = NS(name="terminals", enabled=state != "disabled", purged=state == "purged", removed=state == "removed")
    f = facts_of(ws(addons={"terminals": a}))
    assert "addon:terminals" not in missing_items(f, PROF, INFO)
    assert "addon:worktrees" in missing_items(f, PROF, INFO)


def test_r7_a_grant_ever_issued_by_me_even_revoked_or_expired_is_not_issued_again():
    g = NS(person=ME, revoked=True, expires_at="2020-01-01T00:00:00Z")
    f = facts_of(ws(grants={"gr_1": g}))
    items = missing_items(f, PROF, INFO)
    assert "runner_grant" not in items and "runner_grant_retry" in items  # the deposit was never confirmed
    f2 = facts_of(ws(grants={"gr_1": g}), deposit_confirmed=True)
    assert not {"runner_grant", "runner_grant_retry"} & set(missing_items(f2, PROF, INFO))
    other = NS(person="p_" + "9" * 32, revoked=False, expires_at="2099")
    assert "runner_grant" in missing_items(facts_of(ws(grants={"gr_2": other})), PROF, INFO)


def test_r7_a_setting_that_was_set_is_not_set_again_and_a_runner_that_is_on_stays():
    f = facts_of(ws(settings={"grant_hours": 72}), runner={"enabled": True, "terminals": True})
    items = missing_items(f, PROF, INFO)
    assert "settings" not in items and "runner" not in items


def test_r7_without_the_profile_asking_for_it_nothing_is_missing():
    done = NS(name="dashboard", enabled=True, purged=False, removed=False)
    f = facts_of(ws(addons={"dashboard": done}))
    assert missing_items(f, {**PROF, "addons": ["dashboard"], "agent_grant": False, "agent_hours": 8}, INFO) == []


def test_r7_click_custody_missing_only_when_not_converted():
    p = {**PROF, "method": "click"}
    assert "custody" in missing_items(facts_of(ws()), p, INFO)
    assert "custody" not in missing_items(facts_of(ws(), convenience=True), p, INFO)


# --- the flow in finish mode


@pytest.fixture(autouse=True)
def fake_signer(monkeypatch):
    monkeypatch.setattr(host_signer, "check", lambda *a, **k: None)

    def sign(prepared, *, backend, key_id, workspace_id):
        events = [{**e, "sig": "x"} for e in prepared["events"]]
        secret = GRANT_SECRET if any(e["type"] == "grant.issued" for e in events) else None
        return host_signer.Signed(events, secret)

    monkeypatch.setattr(host_signer, "sign", sign)
    monkeypatch.setattr(
        host_signer,
        "hand_over",
        lambda call, secret, did: {
            "deposit": call("grant.deposit", {"deposit_id": did}, auth={"grant": secret})["data"]["deposit"]
        },
    )


def finish_flow(tmp, ports, facts_box):
    (tmp.work / "config.json").write_text("{}")
    entry = {"workspace_id": WID, "prefix": "ANN", "root": str(tmp.work)}
    env = make_env(
        tmp,
        ports=lambda: ports,
        clock=lambda: ports.now,
        registered=lambda root: entry if root == str(tmp.work) else None,
        read_facts=lambda e: facts_box[0],
    )
    f = SetupFlow(env)
    f.launch(str(tmp.work))
    return f


def mkfacts(**kw):
    base = dict(workspace_id=WID, prefix="ANN", name="Ann", person=ME)
    base.update(kw)
    return Facts(**base)


def test_finish_mode_skips_the_secrets_and_fixes_name_and_prefix(tmp):
    f = finish_flow(tmp, FakePorts(), [mkfacts()])
    st = f.dispatch("GET", "state", {}, None)[1]
    assert st["mode"] == "finish" and st["suggested"] == {"name": "Ann", "prefix": "ANN"}
    assert "addon:terminals" in st["missing"] and "runner_grant" in st["missing"]
    f.dispatch("POST", "profile", {}, PROF)
    assert f.phase == "review"  # no secrets screen
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "secrets/reveal", {}, {})
    assert e.value.code == "setup.phase"
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "profile", {}, {**PROF, "prefix": "OTHER"})
    assert e.value.extra["field"] == "name"


def test_finish_review_has_only_the_missing_items_and_needs_the_tty(tmp):
    box = [
        mkfacts(
            addons=frozenset({"dashboard", "terminals"}),
            runner_enabled=True,
            terminals_enabled=True,
            grant_hours_set=True,
        )
    ]
    f = finish_flow(tmp, FakePorts(), box)
    f.dispatch("POST", "profile", {}, PROF)
    rv = f.dispatch("POST", "review", {}, {})[1]
    assert [i["id"] for i in rv["items"]] == ["addon:worktrees", "runner_grant"]
    assert rv["tty_confirm"] is True and len(rv["fingerprint"]) == 3 and "typed_word" not in rv
    assert f.dispatch("GET", "state", {}, None)[1]["missing"] == ["addon:worktrees", "runner_grant"]


def test_finish_runs_without_genesis_and_with_the_persons_own_custody(tmp):
    ports = FakePorts(FakeTerminal([]))
    box = [mkfacts(addons=frozenset({"dashboard", "terminals"}), runner_enabled=True, grant_hours_set=True)]
    f = finish_flow(tmp, ports, box)
    f.dispatch("POST", "profile", {}, PROF)
    rv = f.dispatch("POST", "review", {}, {})[1]
    ports.terminal.answers[:] = ["yes"]
    ports.expect(f.review.plan)
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = progress(f, out["run_id"])
    n = names(ports)
    assert p["done"] is True and "genesis" not in n and ports.unlocks == 1
    assert p["workspace"] and p["workspace"]["prefix"] == PROF["prefix"]  # the page's Done and the terminal name it
    assert ports.submits == ["addon.granted", "grant.issued"]
    assert [s["id"] for s in p["steps"]] == ["addon:worktrees", "runner_grant"]
    assert ports.passphrase_seen is None  # no new passphrase in finish mode


def test_finish_create_with_changed_facts_asks_for_a_new_review(tmp):
    ports = FakePorts()
    box = [mkfacts()]
    f = finish_flow(tmp, ports, box)
    f.dispatch("POST", "profile", {}, PROF)
    rv = f.dispatch("POST", "review", {}, {})[1]
    box[0] = mkfacts(addons=frozenset({"dashboard", "terminals", "worktrees"}))  # someone granted them meanwhile
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})
    assert e.value.code == "review.changed" and names(ports) == []


def test_finish_never_resurrects_a_revoked_addon_in_the_review(tmp):
    box = [
        mkfacts(
            addons=frozenset({"dashboard", "terminals", "worktrees"}),
            runner_enabled=True,
            grant_hours_set=True,
            grant_issued=True,
            runner_deposit_confirmed=True,
        )
    ]
    f = finish_flow(tmp, FakePorts(), box)
    f.dispatch("POST", "profile", {}, PROF)
    rv = f.dispatch("POST", "review", {}, {})[1]
    assert rv["items"] == [] and rv["runner_grant"] is None


def test_unreadable_facts_refuse_the_launch(tmp):
    (tmp.work / "config.json").write_text("{}")

    def broken(entry):
        raise SetupError("setup.facts_unreadable", "the workspace state could not be read", 500)

    f = SetupFlow(make_env(tmp, registered=lambda r: {"workspace_id": WID}, read_facts=broken))
    with pytest.raises(SetupError) as e:
        f.launch(str(tmp.work))
    assert e.value.code == "setup.facts_unreadable"
    assert fx  # module imported


def test_finish_moves_a_passphrase_key_to_one_click_by_asking_the_passphrase_on_the_terminal(tmp):
    """Regression (found by the real e2e): Finish with a pending conversion has no generated passphrase in memory; it
    used to die on an assert. The person types it on the terminal (echo off); a failure never shows it back."""
    ports = FakePorts(FakeTerminal([]))
    ports.terminal.secrets = ["the-typed-passphrase"]
    box = [mkfacts(addons=frozenset({"dashboard"}), grant_hours_set=True)]
    f = finish_flow(tmp, ports, box)
    f.dispatch(
        "POST",
        "profile",
        {},
        {**PROF, "method": "click", "addons": ["dashboard"], "agent_grant": False, "agent_hours": 8},
    )
    rv = f.dispatch("POST", "review", {}, {})[1]
    ports.terminal.answers[:] = ["yes"]
    ports.expect(f.review.plan)
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = progress(f, out["run_id"])
    n = names(ports)
    assert p["done"] is True and ports.terminal.secret_asked == 1
    assert n.index("stage") < n.index("finish_conversion") and "genesis" not in n


def test_finish_conversion_failure_never_shows_the_typed_passphrase_back(tmp):
    ports = FakePorts(FakeTerminal([]))
    ports.terminal.secrets = ["the-typed-passphrase"]
    ports.fail_stage = 2
    box = [mkfacts(addons=frozenset({"dashboard"}), grant_hours_set=True)]
    f = finish_flow(tmp, ports, box)
    f.dispatch(
        "POST",
        "profile",
        {},
        {**PROF, "method": "click", "addons": ["dashboard"], "agent_grant": False, "agent_hours": 8},
    )
    rv = f.dispatch("POST", "review", {}, {})[1]
    ports.terminal.answers[:] = ["yes"]
    ports.expect(f.review.plan)
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = progress(f, out["run_id"])
    assert p["error"]["code"] == "custody.conversion_failed" and p["error"]["finishable"] is True
    assert not any("the-typed-passphrase" in t for t in ports.terminal.shown)


def _review_rows(tmp, facts, profile):
    f = finish_flow(tmp, FakePorts(), [facts])
    f.dispatch("POST", "profile", {}, profile)
    return f.dispatch("POST", "review", {}, {})[1]


CLICK_PROF = {**PROF, "method": "click", "addons": ["dashboard"], "agent_grant": False, "agent_hours": 8}


def test_m1_one_click_on_a_workspace_whose_sign_methods_leave_click_out_adds_click_first(tmp):
    done = dict(addons=frozenset({"dashboard"}), grant_hours_set=True)
    rv = _review_rows(tmp, mkfacts(**done, sign_methods=("terminal",)), CLICK_PROF)
    row = next(i for i in rv["items"] if i["id"] == "settings")
    assert any("terminal" in ln and "click" in ln for ln in row["lines"] + row["details"])
    assert not any("grant_hours" in ln for ln in row["lines"] + row["details"])  # only what is actually missing
    # never set (click allowed by default) or already allowing click: no settings item
    for methods in (None, ("terminal", "click")):
        rv = _review_rows(tmp, mkfacts(**done, sign_methods=methods), CLICK_PROF)
        assert "settings" not in [i["id"] for i in rv["items"]]


def test_m2_a_runner_grant_retry_row_says_it_re_issues_access(tmp):
    facts = mkfacts(addons=frozenset({"dashboard", "terminals"}), runner_enabled=True, grant_issued=True)
    rv = _review_rows(tmp, facts, {**PROF, "addons": ["dashboard", "terminals"]})
    row = next(i for i in rv["items"] if i["id"] == "runner_grant")
    assert "re-issues agent access" in row["note"]
    fresh = _review_rows(
        tmp,
        mkfacts(addons=frozenset({"dashboard", "terminals"}), runner_enabled=True),
        {**PROF, "addons": ["dashboard", "terminals"]},
    )
    assert "note" not in next(i for i in fresh["items"] if i["id"] == "runner_grant") or "re-issues" not in fresh[
        "items"
    ][-1].get("note", "")


def test_facts_read_the_explicit_sign_methods():
    assert facts_of(ws(settings={"sign_methods": ["terminal"]})).sign_methods == ("terminal",)
    assert facts_of(ws()).sign_methods is None


def test_finish_on_a_complete_workspace_can_be_closed_with_the_dashboard_url(tmp):
    """B12 N9: nothing missing -> state says complete; finish is allowed without a run and ends as done."""
    box = [mkfacts(addons=frozenset({"dashboard", "terminals", "worktrees", "start-agent"}), grant_issued=True)]
    f = finish_flow(tmp, FakePorts(), box)
    st = f.dispatch("GET", "state", {}, None)[1]
    assert st["missing"] == [] and st["complete"] is True
    out = f.dispatch("POST", "finish", {}, {})[1]
    assert out["url"] and out["workspace_id"]
    assert f.finished and f.outcome == "done"
