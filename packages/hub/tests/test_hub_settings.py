"""M4-1 (#1460): ``hub-settings.json`` (0600, validated, no secrets), what ``orch install hub`` seeds, and the read-only
admin route that shows it."""

from __future__ import annotations

import base64
import http.client
import io
import json
import json as _json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from channels_helpers import PW, wall_codes
from webapi_helpers import Web

from orch_hub import restart as RS
from orch_hub import serve as SV
from orch_hub import settings as S
from orch_hub.access import Access
from orch_hub.channels import Channels
from orch_hub.channels.service import CHANNELS_FILE
from orch_hub.contract import audit as AU
from orch_hub.keys import HubKeys
from orch_hub.webapi import settings_api as SA

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
    pairing = w.login(w.admin, "app")  # a phone's pairing session never reaches an admin route
    assert w.call("GET", "/admin/settings", token=pairing)[0] in (401, 403)
    tok = w.login(w.admin)
    s, o, _ = w.call("GET", "/admin/settings", token=tok)
    assert s == 200 and o["configured"] is True
    assert (o["name"], o["company"]) == ("Acme Hub", "Acme AG")
    assert (o["phone_kind"], o["can_change_addresses"], o["restarts"]) == ("private", False, False)
    assert o["addresses"] == [
        {"kind": "private", "web": "http://100.64.1.2:8100"},
        {"kind": "server", "web": "http://127.0.0.1:8100"},
    ]
    assert [m["id"] for m in o["sign_in"]] == ["password"] and o["sign_in"][0]["on"] is True
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


# --- M4 S2 (#1461): name/company and advertised addresses (writes), the restart, `hub_where` -----------------------------


PRIMARY = {"kind": "private", "web": "http://100.64.1.2:8100", "link": "100.64.1.2:8105"}
SERVER = {"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"}
UP = {"v": 1, "name": "Acme Hub", "company": "Acme AG", "addresses": [PRIMARY, SERVER]}
ASSIGNED = {"100.64.1.2", "127.0.0.1", "10.0.0.7", "100.64.9.9"}


def _wired(w, state, restarts=None, bind_all=True, assigned=None):
    """The settings file in place and the control a running hub passes (a restart hook that only counts)."""
    S.save(state, UP)
    w.api.d.settings_file = state / S.SETTINGS_FILE
    seen = restarts if restarts is not None else []
    w.api.d.settings_ctl = SA.SettingsControl(
        bind_all=bind_all, restart=lambda: seen.append(1), assigned=assigned or (lambda ip: ip in ASSIGNED)
    )
    return seen


def _audit_types(w):
    return [r["o"]["type"] for _, r, _e in w.api.d.audit.store.after(0, 1000)]


def _put_general(w, tok, **kw):
    return w.call("PUT", "/admin/settings/general", {"name": "Acme Hub", "company": "Acme AG", **kw}, token=tok)


def _put_addrs(w, tok, *entries):
    return w.call("PUT", "/admin/settings/addresses", {"addresses": list(entries)}, token=tok)


P_ENTRY = {"kind": "private", "host": "100.64.1.2"}
S_ENTRY = {"kind": "server", "host": "127.0.0.1"}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/admin/settings/general", {"name": "x", "company": ""}),
        ("PUT", "/admin/settings/addresses", {"addresses": [{"kind": "private", "host": "100.64.1.2"}]}),
        ("GET", "/admin/settings/addresses/detected", None),
    ],
)
def test_settings_writes_are_admin_only_and_csrf_guarded(w, state, method, path, body):
    _wired(w, state)
    adm = w.login(w.admin)
    assert w.call(method, path, body)[0] == 401
    assert w.call(method, path, body, token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    app = w.login(w.admin, "app")  # a phone's pairing session (bearer) is not the console
    assert w.call(method, path, body, token=app)[0] in (401, 403)
    ticket = w.env.svc.open_session(w.admin)["token"]  # a workspace vouched for it: no password sign-in
    w.browser_tokens.add(ticket)
    assert w.call(method, path, body, token=ticket)[:2] == (403, {"error": "reauth_required"})
    if method == "PUT":
        assert w.call(method, path, body, token=adm, csrf=False)[:2] == (403, {"error": "not_allowed"})
        assert w.call(method, path, body, token=adm, origin="http://evil.example")[:2] == (
            403,
            {"error": "not_allowed"},
        )
        assert w.call(method, path, body, token=adm, origin=None)[:2] == (403, {"error": "not_allowed"})
    assert S.load(state) == UP and _audit_types(w).count("hub_settings") == 0  # nothing changed, nothing audited


def test_general_edit_saves_audits_and_shows_at_once(w, state):
    _wired(w, state)
    tok = w.login(w.admin)
    s, o, _ = _put_general(w, tok, name="  Beta Hub ", company="")
    assert (s, o) == (200, {"ok": True, "name": "Beta Hub", "company": ""})
    assert S.load(state)["name"] == "Beta Hub" and S.load(state)["company"] == ""
    assert S.load(state)["addresses"] == UP["addresses"]  # only the two words changed
    assert (state / S.SETTINGS_FILE).stat().st_mode & 0o777 == 0o600
    recs = [(r["o"], e) for _, r, e in w.api.d.audit.store.after(0, 1000) if r["o"]["type"] == "hub_settings"]
    assert len(recs) == 1 and recs[0][0]["scope_tag"] is None  # hub-wide, in the AUDIT chain (condition 23)
    assert recs[0][1]["change"] == "general" and recs[0][1]["actor_user_id"] == w.admin
    assert set(recs[0][1]) == {"change", "actor_user_id", "nonce"}  # which setting and who: never the value
    assert w.call("GET", "/admin/settings", token=tok)[1]["name"] == "Beta Hub"
    s2, _o2, _ = _put_general(w, tok, name="Beta Hub", company="")  # the same words again: nothing to record
    assert s2 == 200 and _audit_types(w).count("hub_settings") == 1


def test_general_edit_cleans_text_and_refuses_the_rest(w, state):
    _wired(w, state)
    tok = w.login(w.admin)
    s, o, _ = _put_general(w, tok, name="Evil\u202eHub\u200b\x07", company="Co\u2028\nmpany")
    assert (s, o["name"], o["company"]) == (200, "EvilHub", "Company")  # control, bidi, zero-width, separators removed
    for bad in (
        {"name": ""}, {"name": "   "}, {"name": "\u202e\u200b"}, {"name": "x" * 61}, {"name": 5}, {"name": None},
        {"company": "y" * 61}, {"company": 7}, {"name": "\ue000"},
    ):  # fmt: skip
        assert _put_general(w, tok, **bad)[:2] == (400, {"error": "malformed"}), bad
    for body in (
        {"name": "a"},
        {"name": "a", "company": "", "addresses": []},
        {},
        {"name": "a", "company": "", "v": 2},
    ):
        assert w.call("PUT", "/admin/settings/general", body, token=tok)[:2] == (400, {"error": "malformed"}), body
    assert S.load(state)["name"] == "EvilHub"
    for ch in S.load(state)["name"] + S.load(state)["company"]:
        assert ch.isprintable()


def test_general_edit_is_refused_when_the_audit_chain_cannot_be_written(w, state, monkeypatch):
    _wired(w, state)
    tok = w.login(w.admin)

    def boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(w.api.d.audit, "hub_settings", boom)
    assert _put_general(w, tok, name="Changed")[0] == 500
    assert S.load(state) == UP  # the audit is written BEFORE the file: no record, no change


def test_general_without_a_settings_file_is_unavailable(w):
    tok = w.login(w.admin)
    assert _put_general(w, tok)[:2] == (503, {"error": "unavailable"})


def test_address_change_saves_audits_and_restarts_once(w, state):
    restarts = _wired(w, state)
    tok = w.login(w.admin)
    s, o, _ = _put_addrs(w, tok, S_ENTRY, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})
    assert (s, o) == (202, {"ok": True, "changed": True, "restarting": True})
    assert restarts == [1]
    want = [
        PRIMARY,  # the phone address stays first whatever order was asked
        SERVER,
        {"kind": "office", "web": "http://10.0.0.7:8100", "link": "10.0.0.7:8105"},  # ports are the install's
    ]
    assert S.load(state)["addresses"] == want
    last = [e for _, r, e in w.api.d.audit.store.after(0, 1000) if r["o"]["type"] == "hub_settings"]
    assert [(e["change"], e["actor_user_id"]) for e in last] == [("addresses", w.admin)]
    assert S.load(state)["name"] == "Acme Hub"
    # the same list again changes nothing and restarts nothing
    assert _put_addrs(w, tok, P_ENTRY, S_ENTRY, {"kind": "office", "host": "10.0.0.7"})[1]["changed"] is False
    assert restarts == [1] and len(last) == 1


def test_a_public_address_is_not_made_here_and_an_installed_one_is_left_alone(w, state):
    restarts = _wired(w, state)
    tok = w.login(w.admin)
    for pub in (
        {"kind": "public", "host": "hub.example.com"},
        {"kind": "public", "host": "8.8.8.8", "scheme": "https"},
    ):
        assert _put_addrs(w, tok, P_ENTRY, pub)[:2] == (400, {"error": "malformed"})
    assert S.load(state) == UP and restarts == []
    seeded = {"kind": "public", "web": "https://hub.example.com:8100", "link": None}
    S.save(state, {**UP, "addresses": [PRIMARY, SERVER, seeded]})
    # an office address is added; the seeded public entry is kept untouched, asked for or not
    s, o, _ = _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})
    assert s == 202, o
    assert S.load(state)["addresses"][-1] == seeded and len(S.load(state)["addresses"]) == 3
    same = {"kind": "public", "host": "hub.example.com", "scheme": "https"}  # naming it unchanged is fine
    assert _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"}, same)[1]["changed"] is False
    other = {"kind": "public", "host": "evil.example", "scheme": "https"}  # changing it is not
    assert _put_addrs(w, tok, P_ENTRY, other)[:2] == (400, {"error": "malformed"})


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "169.254.1.1",
        "2130706433",
        "0x7f.1",
        "127.1",
        "localhost",
        "::1",
        "fe80::1",
    ],
)
def test_address_change_refuses_unusable_hosts(w, state, host):
    restarts = _wired(w, state, assigned=lambda ip: True)  # even if the computer "owned" it
    tok = w.login(w.admin)
    for kind in ("office", "server", "private"):
        assert _put_addrs(w, tok, P_ENTRY, {"kind": kind, "host": host})[:2] == (400, {"error": "malformed"})
    assert S.load(state) == UP and restarts == [] and "hub_settings" not in _audit_types(w)


def test_an_origin_set_the_next_start_would_refuse_is_not_saved(w, state):
    # behind the edge only https origins are valid: an http office address would crash-loop the restart
    restarts = _wired(w, state)

    def rules(origins, api):
        SV.HubConfig(
            dev=True, listen="127.0.0.1:0", web_listen="127.0.0.1:0", trusted_peers=frozenset({"10.9.9.9"}),
            behind_edge=True, allowed_origins=origins, api_origin=api,
        )  # fmt: skip

    w.api.d.settings_ctl.check_origins = rules
    tok = w.login(w.admin)
    assert _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})[:2] == (400, {"error": "malformed"})
    assert S.load(state) == UP and restarts == [] and "hub_settings" not in _audit_types(w)


def test_serve_still_starts_on_the_file_left_after_a_refused_change(tmp_path):
    state = tmp_path / "hub"
    HubKeys.init(state)
    edge = {"kind": "public", "web": "https://hub.example.com", "link": "127.0.0.1:18105"}
    edge["web"] += ":443"
    S.save(state, {**UP, "addresses": [edge]})
    stop, out, result = threading.Event(), io.StringIO(), []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", "http://127.0.0.1:9", [], web_listen="127.0.0.1:0", behind_edge=True,
                     trusted_peers=frozenset({"127.0.0.1"}), stop=stop, out=out)
        ),
        daemon=True,
    )  # fmt: skip
    t.start()
    end = time.monotonic() + 15
    while "orch-hub web port=" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    assert "orch-hub web port=" in out.getvalue(), out.getvalue()
    stop.set()
    t.join(15)
    assert result == [0]


def test_a_failing_cleanup_exits_non_zero_even_when_a_restart_was_asked_for(tmp_path, monkeypatch):
    from orch_hub.link import LinkServer

    state = tmp_path / "hub"
    HubKeys.init(state)
    S.save(state, UP)
    monkeypatch.setattr(LinkServer, "close", lambda self: (_ for _ in ()).throw(OSError("boom")))
    stop, out, result, execs = threading.Event(), io.StringIO(), [], []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", "http://127.0.0.1:9", [], web_listen="127.0.0.1:0", bind_all=True,
                     stop=stop, out=out, restart_exec=lambda: execs.append(1))
        ),
        daemon=True,
    )  # fmt: skip
    t.start()
    end = time.monotonic() + 15
    while "orch-hub web port=" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    stop.set()
    t.join(15)
    assert result == [1] and execs == []
    assert "close failed" in out.getvalue()


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [P_ENTRY] * 2,  # one entry per kind
        [
            P_ENTRY,
            {"kind": "office", "host": "10.0.0.7"},
            S_ENTRY,
            {"kind": "public", "host": "a.example"},
            {"kind": "x", "host": "b"},
        ],
        [P_ENTRY, {"kind": "lan", "host": "10.0.0.7"}],  # unknown kind
        [P_ENTRY, {"kind": "office", "host": "10.0.0.8"}],  # not an address of this computer
        [P_ENTRY, {"kind": "office", "host": "8.8.8.8"}],  # kind does not fit the address
        [P_ENTRY, {"kind": "private", "host": "100.64.9.9"}],  # a second private one
        [P_ENTRY, {"kind": "public", "host": "localhost"}],
        [P_ENTRY, {"kind": "public", "host": "127.0.0.1"}],
        [P_ENTRY, {"kind": "public", "host": "evil.example/path"}],
        [P_ENTRY, {"kind": "public", "host": "a b.example"}],
        [P_ENTRY, {"kind": "public", "host": "javascript:alert(1)"}],
        [P_ENTRY, {"kind": "public", "host": "hub.example:99"}],
        [P_ENTRY, {"kind": "public", "host": "hub.example", "scheme": "ftp"}],
        [P_ENTRY, {"kind": "office", "host": "10.0.0.7", "scheme": "https"}],
        [
            P_ENTRY,
            {"kind": "office", "host": "10.0.0.7", "web": "http://x:1"},
        ],  # no member of ours but kind, host, scheme
        [P_ENTRY, {"kind": "office"}],
        [P_ENTRY, {"kind": "office", "host": 7}],
        [P_ENTRY, {"kind": "public", "host": "x" * 101}],
        [P_ENTRY, "100.64.9.9"],
    ],
)
def test_address_change_refuses_bad_entries_and_changes_nothing(w, state, entries):
    restarts = _wired(w, state)
    tok = w.login(w.admin)
    s, o, _ = _put_addrs(w, tok, *entries)
    assert (s, o) == (400, {"error": "malformed"})
    assert S.load(state) == UP and restarts == [] and "hub_settings" not in _audit_types(w)


def test_the_phone_address_is_not_changed_here(w, state):
    restarts = _wired(w, state)
    tok = w.login(w.admin)
    for entries in (
        [S_ENTRY],  # dropping the first address
        [{"kind": "private", "host": "100.64.9.9"}, S_ENTRY],  # replacing it
        [{"kind": "office", "host": "10.0.0.7"}],
    ):
        assert _put_addrs(w, tok, *entries)[:2] == (409, {"error": "phone_address_locked"}), entries
    assert S.load(state) == UP and restarts == [] and "hub_settings" not in _audit_types(w)


def test_address_change_needs_a_hub_that_listens_on_every_interface(w, state):
    restarts = _wired(w, state, bind_all=False)
    tok = w.login(w.admin)
    assert _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})[:2] == (503, {"error": "unavailable"})
    assert w.call("GET", "/admin/settings", token=tok)[1]["can_change_addresses"] is False
    w.api.d.settings_ctl = None  # no control at all (a test hub)
    assert _put_addrs(w, tok, P_ENTRY)[:2] == (503, {"error": "unavailable"})
    assert S.load(state) == UP and restarts == []


def test_address_change_audit_failure_keeps_the_old_addresses_and_does_not_restart(w, state, monkeypatch):
    restarts = _wired(w, state)
    tok = w.login(w.admin)
    monkeypatch.setattr(w.api.d.audit, "hub_settings", lambda **kw: (_ for _ in ()).throw(OSError("full")))
    assert _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})[0] == 500
    assert S.load(state) == UP and restarts == []


def test_a_hub_started_by_hand_saves_but_says_it_did_not_restart(w, state):
    _wired(w, state)
    w.api.d.settings_ctl.restart = None
    tok = w.login(w.admin)
    s, o, _ = _put_addrs(w, tok, P_ENTRY, {"kind": "office", "host": "10.0.0.7"})
    assert (s, o) == (200, {"ok": True, "changed": True, "restarting": False})


def test_run18_an_office_or_private_address_that_is_not_this_computers_own_is_refused(w, state):
    restarts = _wired(w, state)  # ASSIGNED is the computer's own list
    tok = w.login(w.admin)
    for entry in ({"kind": "office", "host": "10.77.7.7"}, {"kind": "private", "host": "100.64.77.7"}):
        assert _put_addrs(w, tok, P_ENTRY, entry)[:2] == (400, {"error": "malformed"})
    assert S.load(state) == UP and restarts == [] and "hub_settings" not in _audit_types(w)
    # a member (not an admin) cannot even try
    assert _put_addrs(w, w.login(w.ann), P_ENTRY, {"kind": "office", "host": "10.0.0.7"})[:2] == (
        403,
        {"error": "forbidden_role"},
    )


def test_detected_addresses_are_this_computers_own_with_their_kind(w, state):
    _wired(w, state)
    w.api.d.settings_ctl.detect = lambda: SA.detect_addresses("2: eth0    inet 10.0.0.7/24 brd x scope global\n")
    tok = w.login(w.admin)
    s, o, _ = w.call("GET", "/admin/settings/addresses/detected", token=tok)
    assert (s, o) == (
        200,
        {
            "addresses": [{"ip": "127.0.0.1", "kind": "server"}, {"ip": "10.0.0.7", "kind": "office", "iface": "eth0"}],
            "can_change": True,
        },
    )
    text = "inet 127.0.0.1/8 lo\ninet 100.71.1.5/16 wt0\ninet 169.254.3.3/16 x\ninet 192.168.1.9/24 en0\ninet 8.8.4.4/24 e\ninet 100.71.1.5/16 dup\n"
    assert SA.detect_addresses(text) == [
        {"ip": "127.0.0.1", "kind": "server"},
        {"ip": "100.71.1.5", "kind": "private"},
        {"ip": "192.168.1.9", "kind": "office"},
        {"ip": "8.8.4.4", "kind": "public"},
    ]


def test_run18_detected_addresses_never_lead_with_a_virtual_bridge_and_name_the_interface():
    text = (
        "3: docker0    inet 172.17.0.1/16 brd x scope global docker0\n"
        "4: br-1a2b3c    inet 172.18.0.1/16 brd x scope global br-1a2b3c\n"
        "5: veth9    inet 10.99.0.1/24 brd x scope global veth9\n"
        "2: eth0    inet 192.168.1.9/24 brd x scope global eth0\n"
        "6: virbr0    inet 192.168.122.1/24 brd x scope global virbr0\n"
    )
    got = SA.detect_addresses(text)
    assert [g["ip"] for g in got] == [
        "127.0.0.1",
        "192.168.1.9",
        "172.17.0.1",
        "172.18.0.1",
        "10.99.0.1",
        "192.168.122.1",
    ]
    assert got[1] == {"ip": "192.168.1.9", "kind": "office", "iface": "eth0"}
    ifc = "en0: flags=8863<UP> mtu 1500\n\tinet 10.1.1.4 netmask 0xffffff00\nbridge100: flags=8863<UP>\n\tinet 192.168.64.1 netmask 0xffffff00\n"
    assert [(g["ip"], g.get("iface")) for g in SA.detect_addresses(ifc)[1:]] == [
        ("10.1.1.4", "en0"),
        ("192.168.64.1", "bridge100"),
    ]


def test_the_audit_event_is_closed_and_has_plain_words():
    ok = {"change": "addresses", "actor_user_id": "a" * 32, "nonce": "A" * 43}
    assert AU.check_event("hub_settings", ok) == ok
    for bad in ({**ok, "change": "name"}, {**ok, "value": "x"}, {k: v for k, v in ok.items() if k != "nonce"}):
        with pytest.raises(Exception):  # noqa: B017  # Refused("malformed")
            AU.check_event("hub_settings", bad)
    from orch_hub.activity.feed import AUDIT_WORDS, WORDS

    assert "hub_settings" in AUDIT_WORDS and "hub.settings.changed" in WORDS


# --- the restart (C3): same command line without the origin arguments, no descriptor survives ---------------------------


def test_restart_argv_drops_only_the_origin_arguments():
    orig = ["py", "-m", "orch_hub", "serve", "--state", "s", "--web-origin", "http://a:1", "--api-origin=http://a:1",
            "--web-listen", "1.2.3.4:8100", "--bind-all", "--web-origin=http://b:2"]  # fmt: skip
    assert RS.restart_argv(orig) == [
        "py",
        "-m",
        "orch_hub",
        "serve",
        "--state",
        "s",
        "--web-listen",
        "1.2.3.4:8100",
        "--bind-all",
    ]


def test_exec_self_closes_every_descriptor_and_starts_without_the_old_origins(tmp_path):
    child = textwrap.dedent(
        """
        import os, sys
        open_fds = []
        for fd in range(3, 256):
            try:
                os.fstat(fd)
                open_fds.append(fd)
            except OSError:
                pass
        print(open_fds, sys.argv[1:])
        """
    )
    parent = textwrap.dedent(
        f"""
        import os
        from orch_hub.restart import exec_self
        for _ in range(3):
            fd = os.open({str(tmp_path)!r}, os.O_RDONLY)
            os.set_inheritable(fd, True)   # a descriptor a child would normally keep
        exec_self(["python", "-c", {child!r}, "--api-origin", "http://old:1", "--web-origin=http://old:2", "--keep"])
        """
    )
    r = subprocess.run([sys.executable, "-c", parent], check=False, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[] ['--keep']"


# --- serve: after install the file decides (condition 5), the settings route restarts it --------------------------------


def _http(port, method, path, body=None, cookie=None, origin=None, host=None):
    h = {"X-Orch-Csrf": "1", "Content-Type": "application/json"}
    if host:
        h["Host"] = host
    if origin:
        h["Origin"] = origin
    if cookie:
        h["Cookie"] = cookie
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, "/api/v1" + path, body=None if body is None else _json.dumps(body), headers=h)
        r = c.getresponse()
        return r.status, _json.loads(r.read() or b"{}"), r.getheader("Set-Cookie")
    finally:
        c.close()


@pytest.fixture
def running(tmp_path):
    """A real in-process hub with an admin, a settings file, and start arguments that carry OLD origins."""
    state = tmp_path / "hub"
    keys = HubKeys.init(state)
    acc = Access(str(state / "access.db"))
    user = keys.issue_uk(None)["o"]["user_id"]
    acc.add_user(user, "ann")
    acc.add_member("hub-admin", user)
    ch = Channels(keys, acc, state / CHANNELS_FILE, dev_mode=True)
    ch.set_password(user, PW)
    b32 = ch.new_totp(user)["secret"]
    ch.close()
    acc.close()
    S.save(state, {**UP, "addresses": [{"kind": "server", "web": "http://127.0.0.1:18100", "link": "127.0.0.1:18105"}]})
    stop, out, result, execs = threading.Event(), io.StringIO(), [], []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(
                state, "127.0.0.1:0", "http://127.0.0.1:9", [],
                web_listen="127.0.0.1:0",
                web_origins=frozenset({"http://127.0.0.1:19999"}), api_origin="http://127.0.0.1:19999",  # the OLD arguments
                stop=stop, out=out, restart_exec=lambda: execs.append(1),
            )
        ),
        daemon=True,
    )  # fmt: skip
    t.start()
    end = time.monotonic() + 15
    while "orch-hub web port=" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    port = int(out.getvalue().split("orch-hub web port=")[1].split()[0])
    secret = base64.b32decode(b32 + "=" * (-len(b32) % 8))
    yield {
        "state": state,
        "port": port,
        "user": user,
        "secret": secret,
        "out": out,
        "execs": execs,
        "result": result,
        "t": t,
        "stop": stop,
    }
    stop.set()
    t.join(15)


def _admin_cookie(r):
    origin = f"http://127.0.0.1:{r['port']}"
    (code,) = wall_codes(r["secret"])
    s, o, cookie = _http(
        r["port"], "POST", "/session", {"user_id": r["user"], "password": PW, "totp": code}, origin=origin
    )
    assert s == 200, o
    return cookie.split(";")[0], origin


def test_serve_takes_origins_and_the_api_origin_from_the_file_only(running):
    port, origin = running["port"], f"http://127.0.0.1:{running['port']}"
    assert "ignored" in running["out"].getvalue()  # the old arguments are named as ignored
    s, card, _ = _http(port, "GET", "/hub/card")
    assert (
        s == 200 and card["api_origin"]["o"]["origin"] == "http://127.0.0.1:18100"
    )  # the file's first address, not 19999
    bad = {"user_id": running["user"], "password": "x", "totp": "000000"}
    assert _http(port, "POST", "/session", bad, origin="http://127.0.0.1:19999")[1] == {
        "error": "not_allowed"
    }  # old arg
    assert _http(port, "POST", "/session", bad, origin="http://127.0.0.1:18100")[1] != {
        "error": "not_allowed"
    }  # the file
    assert _http(port, "POST", "/session", bad, origin=origin)[1] != {"error": "not_allowed"}  # the bound listener
    assert _http(port, "POST", "/session", bad, origin="http://evil.example")[1] == {"error": "not_allowed"}


def test_serve_restarts_after_an_address_change_and_closes_everything_first(running):
    cookie, origin = _admin_cookie(running)
    # --bind-all was not given, so this hub cannot advertise another address: the route says so, the file is kept
    s, o, _ = _http(running["port"], "PUT", "/admin/settings/addresses", {"addresses": []}, cookie, origin)
    assert (s, o) == (503, {"error": "unavailable"}) and not running["execs"]
    s, o, _ = _http(running["port"], "PUT", "/admin/settings/general", {"name": "Zed", "company": ""}, cookie, origin)
    assert s == 200 and S.load(running["state"])["name"] == "Zed" and not running["execs"]  # a name needs no restart


def test_serve_with_bind_all_restarts_and_the_next_start_reads_the_new_file(tmp_path):
    state = tmp_path / "hub"
    keys = HubKeys.init(state)
    acc = Access(str(state / "access.db"))
    user = keys.issue_uk(None)["o"]["user_id"]
    acc.add_user(user, "ann")
    acc.add_member("hub-admin", user)
    ch = Channels(keys, acc, state / CHANNELS_FILE, dev_mode=True)
    ch.set_password(user, PW)
    b32 = ch.new_totp(user)["secret"]
    ch.close()
    acc.close()
    first = {"kind": "server", "web": "http://127.0.0.1:18100", "link": "127.0.0.1:18105"}
    office = {"kind": "office", "web": "http://10.0.0.7:18100", "link": "10.0.0.7:18105"}
    S.save(state, {**UP, "addresses": [first, office]})
    out, result, execs = io.StringIO(), [], []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", "http://127.0.0.1:9", [], web_listen="127.0.0.1:0", bind_all=True,
                     out=out, restart_exec=lambda: execs.append(1))
        ),
        daemon=True,
    )  # fmt: skip
    t.start()
    end = time.monotonic() + 15
    while "orch-hub web port=" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    port = int(out.getvalue().split("orch-hub web port=")[1].split()[0])
    origin = f"http://127.0.0.1:{port}"
    (code,) = wall_codes(base64.b32decode(b32 + "=" * (-len(b32) % 8)))
    s, o, cookie = _http(port, "POST", "/session", {"user_id": user, "password": PW, "totp": code}, origin=origin)
    assert s == 200, o
    body = {"addresses": [{"kind": "server", "host": "127.0.0.1"}]}  # drop the office address
    s, o, _ = _http(port, "PUT", "/admin/settings/addresses", body, cookie.split(";")[0], origin)
    assert (s, o["restarting"]) == (202, True)
    t.join(15)
    assert not t.is_alive() and result == [0] and execs == [1]  # serve returned (all closed), THEN the restart ran
    assert [a["kind"] for a in S.load(state)["addresses"]] == ["server"]
    assert SV._link_endpoint(state, "x:1") == "127.0.0.1:18105"  # the connect link follows the file


def test_an_ipv6_phone_address_from_the_install_keeps_every_save_working(w, state):
    v6 = {"kind": "server", "web": "http://[::1]:8100", "link": "[::1]:8105"}
    restarts = _wired(w, state)
    S.save(state, {**UP, "addresses": [v6]})
    tok = w.login(w.admin)
    s, o, _ = _put_addrs(w, tok, {"kind": "server", "host": "::1"}, {"kind": "office", "host": "10.0.0.7"})
    assert s == 202, o
    assert S.load(state)["addresses"][0] == v6 and restarts == [1]  # kept verbatim, then the new one
    gone = {**UP, "addresses": [{**v6, "kind": "office", "web": "http://10.9.9.9:8100", "link": "10.9.9.9:8105"}]}
    S.save(state, gone)  # a primary the machine no longer has: still kept, still saves
    assert _put_addrs(w, tok, {"kind": "office", "host": "10.9.9.9"}, S_ENTRY)[0] == 202


def test_the_real_serve_hook_refuses_an_http_origin_behind_the_edge(tmp_path):
    state = tmp_path / "hub"
    keys = HubKeys.init(state)
    acc = Access(str(state / "access.db"))
    user = keys.issue_uk(None)["o"]["user_id"]
    acc.add_user(user, "ann")
    acc.add_member("hub-admin", user)
    ch = Channels(keys, acc, state / CHANNELS_FILE, dev_mode=True)
    ch.set_password(user, PW)
    b32 = ch.new_totp(user)["secret"]
    ch.close()
    acc.close()
    edge = {"kind": "public", "web": "https://hub.example.com:443", "link": "127.0.0.1:18105"}
    S.save(state, {**UP, "addresses": [edge]})
    stop, out, result, execs = threading.Event(), io.StringIO(), [], []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", "http://127.0.0.1:9", [], web_listen="127.0.0.1:0", bind_all=True,
                     behind_edge=True, trusted_peers=frozenset({"127.0.0.1"}), stop=stop, out=out,
                     restart_exec=lambda: execs.append(1))
        ),
        daemon=True,
    )  # fmt: skip
    t.start()
    end = time.monotonic() + 15
    while "orch-hub web port=" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    port = int(out.getvalue().split("orch-hub web port=")[1].split()[0])
    origin, host = "https://hub.example.com", "hub.example.com"  # what a browser sends: no :443
    (code,) = wall_codes(base64.b32decode(b32 + "=" * (-len(b32) % 8)))
    s, o, cookie = _http(
        port, "POST", "/session", {"user_id": user, "password": PW, "totp": code}, origin=origin, host=host
    )
    assert s == 200, o
    before = (state / S.SETTINGS_FILE).read_bytes()
    body = {
        "addresses": [
            {"kind": "public", "host": "hub.example.com", "scheme": "https"},
            {"kind": "server", "host": "127.0.0.1"},
        ]
    }
    s, o, _ = _http(port, "PUT", "/admin/settings/addresses", body, cookie.split(";")[0], origin, host)
    assert (s, o) == (400, {"error": "malformed"})
    assert (state / S.SETTINGS_FILE).read_bytes() == before and not execs
    stop.set()
    t.join(15)


def test_exec_self_drops_loader_variables_from_the_new_process(tmp_path):
    child = "import os; print(sorted(k for k in os.environ if k.startswith(('PYTHON', 'DYLD_', 'LD_'))), os.environ['KEEP'])"
    parent = textwrap.dedent(
        f"""
        import os
        from orch_hub.restart import exec_self
        os.environ.update(PYTHONPATH=":", DYLD_INSERT_LIBRARIES="/x.dylib", LD_PRELOAD="/y.so", KEEP="1")
        exec_self(["python", "-c", {child!r}])
        """
    )
    r = subprocess.run([sys.executable, "-c", parent], check=False, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[] 1"
