"""M4-1 (#1460): ``hub-settings.json`` (0600, validated, no secrets), what ``orch install hub`` seeds, and the read-only
admin route that shows it."""

from __future__ import annotations

import json
import os

import pytest
from webapi_helpers import Web

from orch_hub import settings as S

GOOD = {
    "v": 1,
    "name": "Acme Hub",
    "company": "Acme AG",
    "addresses": [
        {"kind": "private", "web": "http://100.64.1.2:8100", "link": "100.64.1.2:8105"},
        {"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"},
    ],
}


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def test_save_writes_an_owner_only_file_and_load_reads_it_back(state):
    S.save(state, GOOD)
    assert (state / S.SETTINGS_FILE).stat().st_mode & 0o777 == 0o600
    assert S.load(state) == GOOD
    assert not [p for p in state.iterdir() if p.name.endswith(".tmp")]


def test_an_absent_file_is_none_and_a_loose_or_linked_file_is_refused(state):
    assert S.load(state) is None
    S.save(state, GOOD)
    f = state / S.SETTINGS_FILE
    os.chmod(f, 0o644)
    with pytest.raises(S.SettingsError):
        S.load(state)
    os.chmod(f, 0o600)
    real = state / "real.json"
    f.rename(real)
    f.symlink_to(real)
    with pytest.raises(S.SettingsError):
        S.load(state)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(v=2),
        lambda d: d.update(v=True),
        lambda d: d.update(name=""),
        lambda d: d.update(name="x" * 61),
        lambda d: d.update(name="bad\u202ename"),
        lambda d: d.update(name=" padded"),
        lambda d: d.update(company=None),
        lambda d: d.update(password="hunter2"),  # no member this file does not know
        lambda d: d.pop("company"),
        lambda d: d.update(addresses=[]),
        lambda d: d.update(addresses=GOOD["addresses"] * 3),
        lambda d: d.update(addresses=[{"kind": "lan", "web": "http://10.0.0.1:8100", "link": None}]),
        lambda d: d.update(addresses=[{"kind": "office", "web": "javascript:alert(1)", "link": None}]),
        lambda d: d.update(addresses=[{"kind": "office", "web": "http://10.0.0.1", "link": None}]),
        lambda d: d.update(addresses=[{"kind": "office", "web": "http://10.0.0.1:99999", "link": None}]),
        lambda d: d.update(addresses=[{"kind": "office", "web": "http://10.0.0.1:8100", "link": "bad link"}]),
        lambda d: d.update(addresses=[{"kind": "office", "web": "http://10.0.0.1:8100", "link": None, "key": "x"}]),
        lambda d: d.update(addresses=[GOOD["addresses"][0], GOOD["addresses"][0]]),  # one entry per kind
    ],
)
def test_the_schema_refuses_and_nothing_reaches_the_disk(state, mutate):
    bad = json.loads(json.dumps(GOOD))
    mutate(bad)
    with pytest.raises(S.SettingsError) as e:
        S.save(state, bad)
    assert e.value.code == "bad_settings"
    assert not (state / S.SETTINGS_FILE).exists()


def test_a_damaged_file_is_refused_on_load(state):
    f = state / S.SETTINGS_FILE
    f.write_text('{"v": 1, "name": ')
    os.chmod(f, 0o600)
    with pytest.raises(S.SettingsError):
        S.load(state)


def test_the_literal_that_orch_install_hub_writes_passes_the_hub_validator():
    # the same document as core/tests/install/test_install_cli.py (the two packages share no code, only this shape)
    seeded = {
        "v": 1,
        "name": "Hub",
        "company": "",
        "addresses": [
            {"kind": "private", "web": "http://100.64.1.2:8100", "link": "100.64.1.2:8105"},
            {"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"},
        ],
    }
    assert S.validate(seeded) == seeded


def test_the_route_is_admin_only_read_only_and_carries_no_secret(w, state):
    w.api.d.settings_file = state / S.SETTINGS_FILE
    S.save(state, GOOD)
    s, o, _ = w.call("GET", "/admin/settings", token=w.login(w.ann))
    assert (s, o) == (403, {"error": "forbidden_role"})
    assert w.call("GET", "/admin/settings")[0] == 401
    tok = w.login(w.admin)
    s, o, _ = w.call("GET", "/admin/settings", token=tok)
    assert s == 200 and o["configured"] is True
    assert (o["name"], o["company"], o["fixed_at_install"]) == ("Acme Hub", "Acme AG", True)
    assert o["addresses"] == [
        {"kind": "private", "web": "http://100.64.1.2:8100"},
        {"kind": "server", "web": "http://127.0.0.1:8100"},
    ]
    assert [m["id"] for m in o["sign_in"]] == ["password", "passkey", "company"] and o["sign_in"][0]["on"] is True
    blob = json.dumps(o).lower()
    for word in ('password":', "token", "secret", "key", "setup"):
        assert word not in blob.replace("passkey", "").replace("sign_in", ""), word
    for method in ("PUT", "POST", "DELETE"):  # nothing here changes anything
        assert w.call(method, "/admin/settings", {}, token=tok)[0] in (404, 405)


def test_no_file_and_a_broken_file_answer_not_configured(w, state):
    tok = w.login(w.admin)
    assert w.call("GET", "/admin/settings", token=tok)[1] == {"configured": False, "problem": None}
    w.api.d.settings_file = state / S.SETTINGS_FILE
    assert w.call("GET", "/admin/settings", token=tok)[1] == {"configured": False, "problem": None}
    f = state / S.SETTINGS_FILE
    f.write_text("nope")
    os.chmod(f, 0o600)
    o = w.call("GET", "/admin/settings", token=tok)[1]
    assert o["configured"] is False and o["problem"]


def test_serve_wires_the_settings_file_into_the_web_api():
    import inspect

    from orch_hub import serve

    assert "settings_file=state / SETTINGS_FILE" in inspect.getsource(serve.serve)
