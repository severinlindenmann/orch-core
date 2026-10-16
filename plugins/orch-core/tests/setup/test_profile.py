"""B9 step 2: profile validation, the refused directories, R5 (click + relay)."""

from __future__ import annotations

import os

import pytest

from orch.setup.facts import check_dir
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, get, make_env, post


def err(fn, *a, **k) -> SetupError:
    with pytest.raises(SetupError) as e:
        fn(*a, **k)
    return e.value


def test_state_lists_the_contract_fields(flow):
    st, out = get(flow, "state")
    assert st == 200
    for k in ("phase", "mode", "dir", "dir_name", "suggested", "addons", "methods", "click_available", "click_reason",
              "agent_hours", "relay", "missing"):  # fmt: skip
        assert k in out
    assert out["phase"] == "profile" and out["mode"] == "new" and out["agent_hours"] == [8, 24, 72, 168]
    ids = [a["id"] for a in out["addons"]]
    assert ids[0] == "dashboard" and {"start-agent", "terminals", "worktrees", "quick"} <= set(ids)
    term = next(a for a in out["addons"] if a["id"] == "terminals")
    assert term["kind"] == "addon" and term["capabilities"] == ["pty"]


def test_profile_ok_moves_to_secrets_and_adds_locked_addons(flow):
    st, out = post(flow, "profile", {**PROFILE, "addons": ["terminals"]})
    assert out == {"ok": True} and flow.phase == "secrets"
    assert "dashboard" in flow.profile["addons"]


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"name": ""}, "name"),
        ({"name": "a\nb\x00"}, "name"),
        ({"name": "x" * 81}, "name"),
        ({"prefix": "a"}, "prefix"),
        ({"prefix": "1AB"}, "prefix"),
        ({"prefix": "TOOLONGPFX"}, "prefix"),
        ({"addons": ["nope"]}, "addons"),
        ({"addons": "terminals"}, "addons"),
        ({"method": "touch"}, "method"),
        ({"agent_hours": 9}, "agent_hours"),
        ({"agent_hours": 336}, "agent_hours"),  # B8: agent access is at most 7 days
        ({"agent_hours": True}, "agent_hours"),
        ({"agent_grant": 1}, "agent_grant"),
        ({"relay": "yes"}, "relay"),
    ],
)
def test_closed_schema(flow, patch, field):
    e = err(post, flow, "profile", {**PROFILE, **patch})
    assert e.code == "invalid.input" and e.extra["field"] == field


def test_unknown_and_missing_keys_refused(flow):
    assert err(post, flow, "profile", {**PROFILE, "extra": 1}).code == "invalid.input"
    bad = dict(PROFILE)
    del bad["relay"]
    assert err(post, flow, "profile", bad).extra["field"] == "relay"
    assert err(post, flow, "profile", ["x"]).code == "invalid.input"


def test_r5_click_with_relay_is_rejected_with_its_own_code(tmp):
    f = SetupFlow(make_env(tmp, relay=lambda: {"available": True, "enabled": True}))
    f.launch(str(tmp.work))
    e = err(post, f, "profile", {**PROFILE, "method": "click", "relay": True})
    assert e.code == "click_relay_unsupported" and e.extra["click_reason"] == "relay"
    # terminal + relay is fine when the add-on is available and enabled
    assert post(f, "profile", {**PROFILE, "relay": True})[1] == {"ok": True}


def test_relay_needs_the_addon(flow):
    assert err(post, flow, "profile", {**PROFILE, "relay": True}).extra["field"] == "relay"


def test_click_unavailable_is_refused_with_its_reason(tmp):
    f = SetupFlow(make_env(tmp, click=lambda: (False, "no_keychain")))
    f.launch(str(tmp.work))
    e = err(post, f, "profile", {**PROFILE, "method": "click"})
    assert e.code == "click_unavailable" and e.extra["click_reason"] == "no_keychain"
    assert get(f, "state")[1]["click_available"] is False


def test_reserved_prefix_checked_by_core(tmp):
    from orch.ops.errors import OrchError

    def reserved(root, prefix, name):
        if prefix == "SBX":
            raise OrchError("invalid.input", "SBX is reserved for dev-pair sandboxes")

    f = SetupFlow(make_env(tmp, reserved=reserved))
    f.launch(str(tmp.work))
    e = err(post, f, "profile", {**PROFILE, "prefix": "SBX"})
    assert e.extra["field"] == "prefix" and "reserved" in e.message


def test_a_new_profile_voids_the_review(flow):
    post(flow, "profile", PROFILE)
    flow.review = object()
    post(flow, "profile", {**PROFILE, "agent_hours": 24})
    assert flow.review is None


def test_dir_is_shown_from_core_not_from_the_page(flow, tmp):
    assert get(flow, "state")[1]["dir"] == str(tmp.work) and get(flow, "state")[1]["dir_name"] == "work"


# --- the refused directories


def refused(tmp, d, **kw):
    return err(check_dir, str(d), home=tmp.home, state_dir=tmp.state, config_dirs=(tmp.root / "config",), **kw)


def test_home_ancestors_and_root_refused(tmp):
    assert refused(tmp, tmp.home).code == "setup.dir_refused"
    assert refused(tmp, tmp.root).code == "setup.dir_refused"  # an ancestor of $HOME
    assert refused(tmp, "/").code == "setup.dir_refused"


def test_state_and_config_dirs_refused(tmp):
    for d in (tmp.state, tmp.root / "config"):
        d.mkdir()
        assert refused(tmp, d).code == "setup.dir_refused"
        (d / "inner").mkdir()
        assert refused(tmp, d / "inner").code == "setup.dir_refused"


def test_a_state_dir_inside_the_workspace_is_refused(tmp):
    inner = tmp.work / "state"
    inner.mkdir()
    f = SetupFlow(make_env(tmp, state_dir=inner))
    assert err(f.launch, str(tmp.work)).code == "setup.dir_refused"


def test_symlinked_and_relative_paths_refused(tmp):
    link = tmp.root / "link"
    os.symlink(tmp.work, link)
    assert refused(tmp, link).code == "setup.dir_refused"
    assert refused(tmp, "work").code == "setup.dir_refused"
    assert refused(tmp, tmp.root / "missing").code == "setup.dir_refused"


@pytest.mark.parametrize("name", ["config.json", "keys.jsonl", "events", "tickets", ".state", ".orch"])
def test_dir_with_store_files_refused(tmp, name):
    (tmp.work / name).write_text("x")
    assert refused(tmp, tmp.work).code == "setup.dir_refused"


def test_existing_project_dir_is_fine(tmp):
    (tmp.work / "README.md").write_text("x")
    (tmp.work / "src").mkdir()
    assert check_dir(str(tmp.work), home=tmp.home, state_dir=tmp.state)[1] == "new"


def test_registered_dir_is_finish_mode(tmp):
    (tmp.work / "config.json").write_text("{}")
    mode = check_dir(str(tmp.work), home=tmp.home, state_dir=tmp.state, registered=lambda r: object())[1]
    assert mode == "finish"
