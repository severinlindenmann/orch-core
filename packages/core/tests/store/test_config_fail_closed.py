"""D6 review: an invalid ``config.json`` is refused, never replaced by the defaults (they would widen
``agents.run_for``); a legacy ``icon`` (the signed setting before D6) is accepted and ignored in old events and old
config files."""

from __future__ import annotations

import json

import pytest

from orch import schema
from orch.store import StoreError
from tests.store.test_append import replayed

PAIR = {"icon": "rocket", "colour": "pink"}


def _cfg(env):
    return env.store_dir / "config.json"


def _repo_event(env, name="repo"):
    entry = {"remote": f"https://host/a/{name}", "path": f"../{name}", "default_branch": "main"}
    return env.person_event(env.owner, "workspace", "settings.changed", set={"repos": {name: entry}})


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.pop("agents"), id="no-agents"),
        pytest.param(lambda d: d["agents"].update(run_for="everyone"), id="bad-run_for"),
        pytest.param(lambda d: d["workspace"].update(name=""), id="bad-name"),
    ],
)
def test_a_schema_invalid_config_json_is_refused_and_the_defaults_are_not_applied(env, mutate):
    s = env.bootstrap()
    doc = json.loads(_cfg(env).read_text())
    doc["agents"]["run_for"] = ["owner"]  # narrower than the defaults (owner, maintainer, member)
    _cfg(env).write_text(json.dumps(doc))
    s.append(_repo_event(env, "one"), log="workspace")  # a valid file: its run_for stays
    assert json.loads(_cfg(env).read_text())["agents"]["run_for"] == ["owner"]
    doc = json.loads(_cfg(env).read_text())
    mutate(doc)
    bad = json.dumps(doc).encode()
    _cfg(env).write_bytes(bad)
    with pytest.raises(StoreError) as exc:
        s.append(_repo_event(env, "two"), log="workspace")
    assert exc.value.code == "trust.genesis_mismatch"
    assert "config.json is not valid: " in str(exc.value)
    assert s.scan() == []  # the repair path refuses too (reported): it never rewrites the file from the defaults
    assert any(r.code == "trust.genesis_mismatch" and "config.json is not valid" in r.detail for r in s.reports)
    assert _cfg(env).read_bytes() == bad
    s.close()
    s = env.open()  # a fresh open repairs nothing either
    assert _cfg(env).read_bytes() == bad
    s.close()


def test_an_unparseable_config_json_is_refused_never_rewritten_from_the_defaults(env):
    env.bootstrap().close()
    _cfg(env).write_bytes(b"[" * 100_000)
    s = env.open()
    with pytest.raises(StoreError):
        s.append(_repo_event(env), log="workspace")
    assert s.scan() == []
    assert _cfg(env).read_bytes() == b"[" * 100_000
    s.close()


def test_a_missing_config_json_still_starts_from_the_defaults(env):
    s = env.bootstrap()
    assert json.loads(_cfg(env).read_text())["agents"]["run_for"] == ["owner", "maintainer", "member"]
    s.close()


def test_an_old_settings_changed_with_icon_replays_and_changes_nothing(env):
    s = env.bootstrap()
    before = json.loads(_cfg(env).read_text())
    settings_before = dict(replayed(env).workspace.settings)
    e = env.person_event(env.owner, "workspace", "settings.changed", set={"icon": PAIR})
    s.append(e, log="workspace")  # still a valid event (the append and every replay check the schema)
    e = env.person_event(env.owner, "workspace", "settings.changed", set={"icon": None})
    s.append(e, log="workspace")
    fresh = replayed(env)
    assert not fresh.chain_errors and not fresh.workspace.invalid
    assert dict(fresh.workspace.settings) == settings_before and "icon" not in fresh.workspace.settings
    after = json.loads(_cfg(env).read_text())
    assert "icon" not in after["settings"] and "icon" not in after["workspace"]
    assert after == before
    s.close()


def test_an_old_config_with_settings_icon_validates_and_is_rewritten_without_it(env):
    s = env.bootstrap()
    doc = json.loads(_cfg(env).read_text())
    doc["workspace"]["name"] = "Kept"
    doc["settings"]["icon"] = PAIR
    schema.validate("workspace", doc)  # the legacy key is still valid (optional, ignored)
    _cfg(env).write_text(json.dumps(doc))
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and ev["path"] == "config.json"
    again = json.loads(_cfg(env).read_text())
    assert "icon" not in again["settings"] and again["workspace"]["name"] == "Kept"
    s.close()
