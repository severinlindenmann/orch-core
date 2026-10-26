"""B9 step 4: the review (plain rows via signcontent, the R1 digest, R2 no typed word, R3 fingerprint) and the
signing-time re-check against what the host prepared."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from orch.setup import review as rv
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, make_env, post
from .test_secrets import reveal, words_for


def to_review(flow, profile):
    post(flow, "profile", profile)
    r = reveal(flow)
    post(flow, "secrets/confirm", {"words": words_for(r)})
    return post(flow, "review")[1]


def test_review_needs_confirmed_secrets(flow):
    post(flow, "profile", PROFILE)
    with pytest.raises(SetupError) as e:
        post(flow, "review")
    assert e.value.code == "setup.phase"


def test_review_has_the_contract_fields_and_plain_rows(flow):
    out = to_review(flow, {**PROFILE, "addons": ["dashboard", "terminals", "worktrees"]})
    for k in (
        "review_id",
        "digest",
        "fingerprint",
        "items",
        "runner_grant",
        "flags",
        "risk",
        "tty_confirm",
    ):
        assert k in out
    assert "typed_word" not in out
    ids = [i["id"] for i in out["items"]]
    assert ids == ["addon:dashboard", "addon:terminals", "addon:worktrees"]
    term = out["items"][1]
    assert term["lines"][0] == "Turn on Terminals: it says it will open terminals on this computer, as you"
    assert not any("capabilities" in line or "pty" in line for line in term["lines"])  # in words, not names
    assert any(
        "capabilities" in line and "pty" in line for line in term["details"]
    )  # every signed value is in the details
    assert out["digest"].startswith("sha256:") and out["runner_grant"] is None and out["risk"] is None


def test_terminal_default_has_no_settings_item_and_no_word(flow):
    out = to_review(flow, {**PROFILE, "addons": ["dashboard"]})
    # the dashboard is locked on: its grant is the one item of a default workspace (it is what makes the workspace show)
    assert [i["id"] for i in out["items"]] == ["addon:dashboard"]
    assert out["tty_confirm"] is False
    assert out["fingerprint"] is None


def test_r2_long_agent_hours_and_a_runner_grant_need_no_typed_word(flow):
    out = to_review(flow, {**PROFILE, "agent_hours": 24})
    assert "typed_word" not in out
    assert [i["id"] for i in out["items"]][:1] == ["settings"]
    row = out["items"][0]
    assert any("grant_hours" in line and "24" in line for line in row["lines"])
    f2 = rev_flow(flow)
    out = to_review(f2, {**PROFILE, "agent_grant": True, "agent_hours": 8, "addons": ["dashboard", "start-agent"]})
    assert "typed_word" not in out and out["tty_confirm"] is True and len(out["fingerprint"]) == 3
    assert out["runner_grant"] == {"hours": 8, "scope": "workable", "holder": "this machine's runner, never shown"}
    grant = next(i for i in out["items"] if i["id"] == "runner_grant")
    assert "grant: set at signing" in grant["volatile"]
    assert any(rv.PLACEHOLDER in line for line in grant["lines"])


def rev_flow(flow):
    # a fresh flow over the same directory
    f = SetupFlow(flow.e)
    f.launch(flow.dir)
    return f


def test_runner_grant_over_24h_has_an_explicit_days_row(flow, monkeypatch):
    from orch.model import types

    monkeypatch.setattr(types, "GRANT_MAX_HOURS", 168, raising=False)  # B8 lifted the runner cap
    out = to_review(flow, {**PROFILE, "agent_grant": True, "agent_hours": 72, "addons": ["dashboard", "start-agent"]})
    grant = next(i for i in out["items"] if i["id"] == "runner_grant")
    assert grant["note"] == "Agents may work for 3 days on this machine."
    assert out["runner_grant"]["hours"] == 72


def test_digest_binds_the_profile_and_the_dir(tmp):
    def digest(profile, name="w"):
        d = tmp.root / name
        d.mkdir(exist_ok=True)
        f = SetupFlow(make_env(tmp))
        f.launch(str(d))
        return to_review(f, profile)["digest"]

    base = digest(PROFILE)
    assert digest(PROFILE) == base
    assert digest({**PROFILE, "name": "Bob"}) != base
    assert digest({**PROFILE, "prefix": "BOB"}) != base
    assert digest({**PROFILE, "agent_hours": 24}) != base
    assert digest({**PROFILE, "addons": ["dashboard", "worktrees"]}) != base
    assert digest(PROFILE, "other") != base


def test_digest_includes_the_manifest_hashes(tmp):
    from orch.addons import builtin

    def digest(raw):
        f = SetupFlow(make_env(tmp, manifest_bytes=lambda n: raw if n == "terminals" else builtin.manifest_bytes(n)))
        f.launch(str(tmp.work))
        return to_review(f, {**PROFILE, "addons": ["dashboard", "terminals"]})["digest"]

    import json

    doc = json.loads(builtin.manifest_bytes("terminals"))
    other = json.dumps({**doc, "version": "9.9.9"}).encode()
    assert digest(builtin.manifest_bytes("terminals")) != digest(other)


def test_click_path_review_has_settings_and_the_risk(tmp):
    f = SetupFlow(make_env(tmp))
    f.launch(str(tmp.work))
    out = to_review(f, {**PROFILE, "method": "click"})
    assert out["risk"]["once"] is True and "Convenience signing" in out["risk"]["text"]
    settings = out["items"][0]
    assert settings["id"] == "settings" and any("sign_methods" in line for line in settings["lines"])
    assert {"id": "custody", "text": "Approvals on this machine need one click, with no passphrase."} in out["flags"]


def test_a_new_review_has_a_new_id_and_a_profile_post_voids_it(flow):
    a = to_review(flow, PROFILE)
    b = post(flow, "review")[1]
    assert a["review_id"] != b["review_id"] and a["digest"] == b["digest"]
    post(flow, "profile", {**PROFILE, "agent_hours": 24})
    assert flow.review is None


# --- signing-time re-check (R1)


def planned(profile=None, **kw):
    from orch.addons import builtin
    from orch.setup.facts import catalog

    prof = {**PROFILE, "addons": ["dashboard", "terminals", "start-agent"], "agent_grant": True, **(profile or {})}
    info = {a.id: a for a in catalog()}
    p = rv.plan(prof, "/w", info, None, manifest_bytes=builtin.manifest_bytes)
    return p


NOW = 1_800_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepared_for(p, now=NOW):
    out = []
    for it in p.items:
        ev = copy.deepcopy(it.event)
        ev.update({"v": 2, "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "actor": {}, "auth": "passphrase", "roster_v": 0})
        if ev["type"] == "grant.issued":
            ev.update(
                grant="gr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                issued_at=iso(now),
                expires_at=iso(now + 3600 * it.hours),
                secret_hash=None,
            )
        out.append([ev])
    return out


def test_honest_prepare_passes():
    p = planned()
    rv.check_prepared(p.items, p.rows, prepared_for(p), NOW)


def mismatch(p, prepared):
    with pytest.raises(SetupError) as e:
        rv.check_prepared(p.items, p.rows, prepared, NOW)
    assert e.value.code == "review.mismatch"
    return e.value


def test_extra_event_or_missing_event_aborts():
    p = planned()
    pre = prepared_for(p)
    mismatch(p, pre[:-1])
    mismatch(p, [*pre, pre[0]])
    mismatch(p, [[*pre[0], pre[0][0]], *pre[1:]])


def test_a_different_capability_aborts():
    p = planned()
    pre = prepared_for(p)
    i = next(i for i, it in enumerate(p.items) if it.id == "addon:terminals")
    pre[i][0]["capabilities"] = ["pty", "spawn_agent"]
    mismatch(p, pre)


def test_an_unplanned_field_aborts():
    p = planned()
    pre = prepared_for(p)
    pre[0][0]["sneaky"] = "x"
    mismatch(p, pre)


def test_different_hours_scope_label_or_verbs_abort():
    p = planned()
    for key, value in (("hours", 99), ("scope", "all"), ("label", "x"), ("verbs", ["log"])):
        pre = prepared_for(p)
        g = next(e for ev in pre for e in ev if e["type"] == "grant.issued")
        g[key] = value
        mismatch(p, pre)


def test_volatile_fields_are_checked_by_shape():
    p = planned()
    for patch in (
        {"grant": "not-a-grant"},
        {"issued_at": iso(NOW + 100000)},  # far from now
        {"expires_at": iso(NOW + 5)},  # not issued_at + hours
        {"issued_at": "tomorrow"},
    ):
        pre = prepared_for(p)
        g = next(e for ev in pre for e in ev if e["type"] == "grant.issued")
        g.update(patch)
        mismatch(p, pre)


def test_the_event_type_cannot_change():
    p = planned()
    pre = prepared_for(p)
    pre[0][0]["type"] = "member.removed"
    mismatch(p, pre)


def test_an_event_the_view_cannot_render_is_a_mismatch_not_a_pass(monkeypatch):
    p = planned()
    pre = prepared_for(p)
    monkeypatch.setattr(
        rv, "render_events", lambda events, **k: (_ for _ in ()).throw(SetupError("review.unrenderable", "x", 500))
    )
    with pytest.raises(SetupError):
        rv.check_prepared(p.items, p.rows, pre, NOW)


def test_the_builtin_dashboard_manifest_is_the_dashboards_own_file():
    """The setup grants the dashboard from the copy that ships in orch-core: it must not drift from the add-on's."""
    from pathlib import Path

    from orch.addons import builtin

    own = Path(__file__).resolve().parents[4] / "addons" / "dashboard" / "orch-addon.json"
    if not own.is_file():
        pytest.skip("not in the monorepo checkout")
    assert builtin.manifest_bytes("dashboard") == own.read_bytes()


def test_review_names_the_workspace_it_creates(flow):
    """B12 N7: short name, person and folder come from core's review (and its digest), not from page state."""
    out = to_review(flow, PROFILE)
    ws = out["workspace"]
    assert ws["prefix"] == PROFILE["prefix"] and ws["name"] == PROFILE["name"] and ws["dir"] == flow.dir
    assert ws["workspace_name"] == PROFILE["workspace_name"]
    assert ws["lines"][0] == f"Workspace {PROFILE['workspace_name']} ({PROFILE['prefix']}) for {PROFILE['name']}"
    assert f"Folder: {flow.dir}" in ws["lines"]
