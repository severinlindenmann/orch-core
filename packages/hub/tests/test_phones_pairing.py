"""M2-10 (#1427 #1428 #1429): pair a phone from the console, the Phones & browsers data, the old-app flag.

Opus conditions 18-20, each pinned here:
* 18: a person's own views are scoped by the SESSION user on the server, never by a request field.
* 19: ``POST /me/pairing`` needs password + the CURRENT code through the same caps and lockouts as a browser key
  registration, makes a code bound to that user, single use, at most 5 minutes, one live per user, never recorded in the
  activity history, and the new phone is announced on the person's other channels.
* 20: ``app_version`` is untrusted display text: sanitised, dropped (not refused) when odd, never a gate."""

from __future__ import annotations

import json

import pytest
from channels_helpers import new_channel, prove
from webapi_helpers import ORIGIN, Web

from orch_hub.activity import ActivityLog
from orch_hub.channels.service import CONSOLE_PAIRING_TTL_MS, PAIRING_TTL_MS, clean_app_version
from orch_hub.channels.store import ChannelsDB
from orch_hub.contract import channelapi as CA
from orch_hub.contract.canon import b64u
from orch_hub.versions import MIN_PHONE_VERSION

MIN = 60_000


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.api.d.api_origin = ORIGIN
    x.api.d.activity = ActivityLog(tmp_path / "activity.db", now_ms=x.clock)
    yield x
    x.api.d.activity.close()
    x.close()


def issue(w: Web, tok: str, user: str, **kw):
    return w.call("POST", "/me/pairing", w.creds(user), token=tok, **kw)


def code_of(w: Web, tok: str, user: str) -> str:
    s, o, _ = issue(w, tok, user)
    assert s == 200, o
    return o["code"]


def register_phone(w: Web, user: str, code: str, *, label: str = "My phone", begin=None, complete=None, signer=None):
    """A phone: app sign-in (pairing session), register with the code. Returns (status, body, (signer, token))."""
    signer = signer or new_channel()
    tok = w.login(user, "app")
    s, begun, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(signer.public), "pairing_code": code, "label": label, **(begin or {})},
        token=tok,
    )
    if s != 200:
        return s, begun, (signer, tok)
    s, got, _ = w.call(
        "POST",
        "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(signer, begun), **(complete or {})},
        token=tok,
    )
    return s, got, (signer, tok)


def mine(w: Web, tok: str):
    s, o, _ = w.call("GET", "/me/channels", token=tok)
    assert s == 200, o
    return o


# --- condition 19: password + current code, same caps and lockouts -------------------------------------------------------------


def test_the_code_is_issued_only_for_password_and_a_current_totp(w):
    tok = w.login(w.ann)
    assert w.call("POST", "/me/pairing", None, token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("POST", "/me/pairing", {"password": "correct horse battery"}, token=tok)[1] == {"error": "malformed"}
    assert w.call("POST", "/me/pairing", {"totp": w.env.next_code(w.ann)}, token=tok)[1] == {"error": "malformed"}
    wrong_pw = {"password": "not the password", "totp": w.env.next_code(w.ann)}
    assert w.call("POST", "/me/pairing", wrong_pw, token=tok)[:2] == (401, {"error": "bad_credentials"})
    wrong_code = {"password": "correct horse battery", "totp": "abcdef"}
    assert w.call("POST", "/me/pairing", wrong_code, token=tok)[:2] == (401, {"error": "bad_credentials"})
    assert w.env.db_count("pairing", w.ann) == 0  # nothing was made by any refusal
    s, o, _ = issue(w, tok, w.ann)
    assert s == 200 and o["expires_ms"] > w.clock() and o["code"]


def test_a_code_that_signed_in_a_moment_ago_is_not_current(w):
    tok = w.login(w.ann)
    reused = {"password": "correct horse battery", "totp": w.env.code(w.ann)}  # the step the sign-in just spent
    assert w.call("POST", "/me/pairing", reused, token=tok)[:2] == (401, {"error": "bad_credentials"})
    assert w.env.db_count("pairing", w.ann) == 0


def test_wrong_attempts_are_counted_by_the_same_lockouts_as_sign_in(w):
    tok = w.login(w.ann)
    bad = {"password": "correct horse battery", "totp": "abcdef"}
    for _ in range(5):
        assert w.call("POST", "/me/pairing", bad, token=tok)[1] == {"error": "bad_credentials"}
    s, o, _ = issue(w, tok, w.ann)  # now even the right password and code are refused
    assert (s, o) == (429, {"error": "rate_limited"})
    assert w.env.db_count("pairing", w.ann) == 0
    # the counter is the one sign-in uses: the same person from the same address cannot sign in either
    s, o, _ = w.call(
        "POST",
        "/session",
        {"user_id": w.ann, "password": "correct horse battery", "totp": w.env.next_code(w.ann), "client": "browser"},
    )
    assert s != 200 and o["error"] in ("rate_limited", "bad_credentials")
    w.clock.advance(16 * MIN)  # the window passes and the lock ends by itself
    assert issue(w, w.login(w.ann), w.ann)[0] == 200


def test_issuing_goes_through_the_guarded_credential_check_for_the_session_user(w):
    tok = w.login(w.ann)
    seen = []
    guarded = w.api._guarded_credentials
    w.api._guarded_credentials = lambda c, user, check: (seen.append(user), guarded(c, user, check))[1]
    assert issue(w, tok, w.ann)[0] == 200
    assert seen == [w.ann]  # the per-source caps, the hashing slot and the lockouts of a key registration


# --- condition 19: scope of the caller ---------------------------------------------------------------------------------------


def test_only_a_real_browser_session_can_issue(w):
    # a switch-ticket session (a workspace vouched for it, no password sign-in) is told to sign in for real
    sw = w.env.svc.open_session(w.ann)["token"]
    w.browser_tokens.add(sw)
    assert w.call("POST", "/me/pairing", w.creds(w.ann), token=sw)[:2] == (403, {"error": "reauth_required"})
    # a pairing session and a channel session (the app) may not
    ptok = w.login(w.ann, "app")
    assert w.call("POST", "/me/pairing", w.creds(w.ann), token=ptok)[:2] == (403, {"error": "forbidden_role"})
    code = code_of(w, w.login(w.ann), w.ann)
    _, _, (signer, _) = register_phone(w, w.ann, code)
    ctok = _bind(w, signer, w.ann)
    assert w.call("POST", "/me/pairing", w.creds(w.ann), token=ctok)[:2] == (403, {"error": "forbidden_role"})
    # the console's own cookie session lifted into a script as a bearer never mints
    btok = w.login(w.ann)
    assert w.call("POST", "/me/pairing", w.creds(w.ann), token=btok, cookie=False)[:2] == (
        403,
        {"error": "not_allowed"},
    )
    # no session at all
    assert w.call("POST", "/me/pairing", w.creds(w.ann))[:2] == (401, {"error": "no_session"})
    assert w.env.db_count("pairing", w.ann) == 0


def _bind(w: Web, signer, user: str) -> str:
    """Bind a fresh pairing session of an already registered phone: returns its channel session token."""
    from test_phone_routes import bind

    tok = w.login(user, "app")
    cid = next(c["channel_id"] for c in w.env.svc.channels(user) if c["kind"] == "phone")
    s, o, _ = bind(w, signer, cid, user, tok)
    assert s == 200, o
    return o["token"]


def test_the_code_is_for_the_session_user_and_ignores_the_body(w):
    tok = w.login(w.ann)
    s, o, _ = w.call("POST", "/me/pairing", {**w.creds(w.ann), "user_id": w.admin}, token=tok)
    assert s == 200
    assert w.env.db_count("pairing", w.ann) == 1 and w.env.db_count("pairing", w.admin) == 0
    # the admin cannot use it for the admin's own account
    s, o2, _ = register_phone(w, w.admin, o["code"])
    assert (s, o2) == (400, {"error": "bad_pairing_code"})


# --- condition 19: the code ---------------------------------------------------------------------------------------------------


def test_the_code_lives_at_most_five_minutes_by_the_clock(w):
    assert CONSOLE_PAIRING_TTL_MS <= 300_000 and PAIRING_TTL_MS > CONSOLE_PAIRING_TTL_MS
    tok = w.login(w.ann)
    s, o, _ = issue(w, tok, w.ann)
    assert s == 200 and o["expires_ms"] - w.clock() <= 300_000
    row = w.env.svc.db.q("SELECT expires_ms, source FROM pairing WHERE user_id=?", (w.ann,))
    assert row == [(o["expires_ms"], "console")]
    # still good a little before the end ...
    w.clock.advance(o["expires_ms"] - w.clock() - 90_000)
    ok, got, _ = register_phone(w, w.ann, o["code"])
    assert ok == 200, got
    # ... and refused after it (a new code, the same age)
    s, o, _ = issue(w, tok, w.ann)
    w.clock.advance(300_001)
    s, got, _ = register_phone(w, w.ann, o["code"])
    assert (s, got) == (400, {"error": "bad_pairing_code"})


def test_the_operator_code_keeps_its_own_longer_life_and_is_left_alone(w):
    cli = w.env.svc.create_pairing_code(w.ann)
    assert cli["expires_ms"] - w.clock() == PAIRING_TTL_MS
    code_of(w, w.login(w.ann), w.ann)
    assert sorted(w.env.svc.db.q("SELECT source FROM pairing WHERE user_id=?", (w.ann,))) == [("cli",), ("console",)]
    s, got, _ = register_phone(w, w.ann, cli["code"])  # the operator's code still works
    assert s == 200, got


def test_a_code_works_once(w):
    code = code_of(w, w.login(w.ann), w.ann)
    s, got, _ = register_phone(w, w.ann, code)
    assert s == 200, got
    s, got, _ = register_phone(w, w.ann, code)
    assert (s, got) == (400, {"error": "bad_pairing_code"})
    assert len(w.env.svc.channels(w.ann)) == 1
    assert w.env.db_count("pairing", w.ann) == 0


def test_two_registrations_opened_with_one_code_cannot_both_finish(w):
    code = code_of(w, w.login(w.ann), w.ann)
    a, b = new_channel(), new_channel()
    toks, begun = [], []
    for k in (a, b):
        tok = w.login(w.ann, "app")
        s, o, _ = w.call(
            "POST",
            "/channels/register/begin",
            {"kind": "phone", "key_pub": b64u(k.public), "pairing_code": code},
            token=tok,
        )
        assert s == 200, o
        toks.append(tok)
        begun.append(o)
    got = []
    for k, tok, o in zip((a, b), toks, begun, strict=True):
        got.append(
            w.call(
                "POST",
                "/channels/register/complete",
                {"challenge_id": o["challenge_id"], "signed": prove(k, o)},
                token=tok,
            )[0]
        )
    assert sorted(got) == [200, 400]
    assert len(w.env.svc.channels(w.ann)) == 1


def test_the_code_is_bound_to_the_user_who_issued_it(w):
    code = code_of(w, w.login(w.ann), w.ann)
    for user in (w.admin,):  # another person, signed in as themself
        s, got, _ = register_phone(w, user, code)
        assert (s, got) == (400, {"error": "bad_pairing_code"})
        assert w.env.svc.channels(user) == []
        # also from a browser session of the other person
        tok = w.login(user)
        s, got, _ = w.call(
            "POST",
            "/channels/register/begin",
            {"kind": "phone", "key_pub": b64u(new_channel().public), "pairing_code": code},
            token=tok,
        )
        assert (s, got) == (400, {"error": "bad_pairing_code"})
    s, got, _ = register_phone(w, w.ann, code)  # the failed tries did not burn it
    assert s == 200, got


def test_a_new_code_replaces_the_old_one(w):
    tok = w.login(w.ann)
    first = code_of(w, tok, w.ann)
    second = code_of(w, tok, w.ann)
    assert first != second
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pairing WHERE user_id=? AND source='console'", (w.ann,)) == [(1,)]
    assert register_phone(w, w.ann, first)[:2] == (400, {"error": "bad_pairing_code"})
    s, got, _ = register_phone(w, w.ann, second)
    assert s == 200, got


def test_a_registration_opened_with_the_replaced_code_cannot_finish(w):
    tok = w.login(w.ann)
    first = code_of(w, tok, w.ann)
    k = new_channel()
    ptok = w.login(w.ann, "app")
    s, begun, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(k.public), "pairing_code": first},
        token=ptok,
    )
    assert s == 200
    code_of(w, tok, w.ann)  # replaces it while the phone is mid-way
    s, got, _ = w.call(
        "POST",
        "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(k, begun)},
        token=ptok,
    )
    assert (s, got) == (400, {"error": "bad_pairing_code"})
    assert w.env.svc.channels(w.ann) == []


def test_the_answer_has_the_link_the_operator_tool_builds(w):
    s, o, _ = issue(w, w.login(w.ann), w.ann)
    assert s == 200
    assert o["address"] == ORIGIN
    assert o["link"] == f"orch://hub-pair/127.0.0.1:9000#h1.{w.env.keys.hub_id}.{w.ann}.{o['code']}"
    assert CA.parse_pair_link(o["link"], dev_mode=True)["code"] == o["code"]
    assert o["fingerprint"] == CA.fingerprint(w.env.keys.hub_id)
    w.api.d.api_origin = None  # no configured origin: the code still works, no link and no address
    s, o, _ = issue(w, w.login(w.ann), w.ann)
    assert s == 200 and "link" not in o and "address" not in o and o["code"]


# --- condition 19: the activity history ------------------------------------------------------------------------------------------


def test_issued_and_used_are_recorded_without_the_code(w):
    code = code_of(w, w.login(w.ann), w.ann)
    s, got, _ = register_phone(w, w.ann, code)
    assert s == 200, got
    cid = got["channel_cert"]["o"]["channel_id"]
    rows = w.api.d.activity.list()
    assert sorted(r["type"] for r in rows) == ["pairing.issued", "pairing.used"]
    for r in rows:
        assert r["actor_id"] == w.ann and r["subject_user"] == w.ann
    used = next(r for r in rows if r["type"] == "pairing.used")
    assert used["detail"] == {"channel_id": cid}
    blob = json.dumps(rows) + json.dumps(w.env.svc.events()) + json.dumps(w.env.svc.channels(w.ann))
    assert code not in blob and code.replace("-", "") not in blob
    s, feed, _ = w.call("GET", "/admin/activity", token=w.login(w.admin))
    assert s == 200 and code not in json.dumps(feed)


def test_an_operator_code_is_not_recorded_as_a_console_pairing(w):
    code = w.env.svc.create_pairing_code(w.ann)["code"]
    s, got, _ = register_phone(w, w.ann, code)
    assert s == 200, got
    assert w.api.d.activity.list() == []


def test_a_refused_issue_records_nothing(w):
    tok = w.login(w.ann)
    w.call("POST", "/me/pairing", {"password": "nope nope nope", "totp": "abcdef"}, token=tok)
    assert w.api.d.activity.list() == []


# --- condition 19: the new phone is announced -------------------------------------------------------------------------------


def test_a_new_phone_is_announced_to_that_person_only(w):
    ann, adm = w.login(w.ann), w.login(w.admin)
    assert mine(w, ann)["new_channels"] == []
    s, got, (signer, _) = register_phone(w, w.ann, code_of(w, ann, w.ann), label="Ann's iPhone")
    assert s == 200, got
    cid = got["channel_cert"]["o"]["channel_id"]
    seen = mine(w, ann)["new_channels"]
    assert seen == [
        {"channel_id": cid, "label": "Ann's iPhone", "kind": "phone", "created_ms": w.clock(), "seen": False}
    ]
    assert mine(w, adm)["new_channels"] == []  # nobody else is told, and nobody else's list shows it
    assert cid not in [c["channel_id"] for c in mine(w, adm)["channels"]]
    # the new phone's own session cannot read the notice or dismiss it
    ctok = _bind(w, signer, w.ann)
    assert mine(w, ctok)["new_channels"] == []
    assert w.call("POST", "/me/channels/seen", {}, token=ctok)[:2] == (403, {"error": "forbidden_role"})
    assert len(mine(w, ann)["new_channels"]) == 1
    # another person dismissing clears nothing of ann's
    assert w.call("POST", "/me/channels/seen", {}, token=adm)[0] == 200
    assert len(mine(w, ann)["new_channels"]) == 1
    # ann dismisses: gone
    assert w.call("POST", "/me/channels/seen", {"user_id": w.admin}, token=ann)[0] == 200
    assert mine(w, ann)["new_channels"] == []


def test_a_removed_phone_drops_out_of_the_notice_and_the_removal_is_recorded(w):
    ann = w.login(w.ann)
    _, got, _ = register_phone(w, w.ann, code_of(w, ann, w.ann))
    cid = got["channel_cert"]["o"]["channel_id"]
    assert len(mine(w, ann)["new_channels"]) == 1
    assert w.call("POST", f"/channels/{cid}/revoke", {}, token=ann)[0] == 200
    assert mine(w, ann)["new_channels"] == []
    assert w.api.d.activity.list()[0]["type"] == "channel.revoked"  # newest first


# --- condition 18: scoping by the session user -------------------------------------------------------------------------------------


def test_my_channels_is_the_session_users_whatever_the_request_says(w):
    ann, adm = w.login(w.ann), w.login(w.admin)
    _, ac = w.register_channel(ann)
    _, bc = w.register_channel(adm)
    for q, body in (
        (f"?user_id={w.admin}", None),
        (f"?user_id={w.admin}&all=1", None),
        ("", {"user_id": w.admin}),
        (f"?user_id={w.admin}", {"user_id": w.admin, "channels": "all"}),
    ):
        s, o, _ = w.call("GET", "/me/channels" + q, body, token=ann)
        assert s == 200
        assert [c["channel_id"] for c in o["channels"]] == [ac] and {c["user_id"] for c in o["channels"]} == {w.ann}
    s, o, _ = w.call("GET", "/me/channels?user_id=" + w.ann, token=adm)
    assert [c["channel_id"] for c in o["channels"]] == [bc]
    # a malformed id in the query is not even looked at
    assert w.call("GET", "/me/channels?user_id=not-an-id", token=ann)[0] == 200


def test_a_person_cannot_read_every_ones_channels_or_remove_anyone_elses(w):
    ann, adm = w.login(w.ann), w.login(w.admin)
    _, bc = w.register_channel(adm)
    for path in ("/admin/channels", f"/admin/channels?user_id={w.admin}", f"/admin/channels?user_id={w.ann}"):
        assert w.call("GET", path, token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.call("POST", f"/admin/channels/{bc}/revoke", {}, token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.call("POST", f"/channels/{bc}/revoke", {}, token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.env.svc.channel_state(w.admin, bc) == "active"
    # the admin sees everyone's, and may remove any
    _, ac = w.register_channel(ann)
    s, o, _ = w.call("GET", "/admin/channels", token=adm)
    assert s == 200 and {c["user_id"] for c in o["channels"]} == {w.ann, w.admin}
    s, o, _ = w.call("GET", f"/admin/channels?user_id={w.ann}", token=adm)
    assert [c["channel_id"] for c in o["channels"]] == [ac]
    assert w.call("POST", f"/admin/channels/{ac}/revoke", {}, token=adm)[0] == 200
    assert w.env.svc.channel_state(w.ann, ac) == "revoked"


# --- condition 20: the app version ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["0.1.0", "1.2.3-beta.4+build7", "2", "a" * 32, "0.0.9"])
def test_a_clean_version_is_kept(good):
    assert clean_app_version(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a" * 33,
        "1.0 beta",
        "<b>1</b>",
        "1.0\n",
        "1.0\u202e",
        "1_0",
        "v1/2",
        "\u0661.0",
        5,
        None,
        ["1.0"],
        {"v": 1},
        True,
    ],
)
def test_anything_else_is_dropped_not_refused(bad):
    assert clean_app_version(bad) is None


def test_the_phone_reports_a_version_at_begin_or_complete(w):
    ann = w.login(w.ann)
    s, _, _ = register_phone(w, w.ann, code_of(w, ann, w.ann), begin={"app_version": "0.2.0"})
    assert s == 200
    assert [c["app_version"] for c in mine(w, ann)["channels"]] == ["0.2.0"]
    s, _, _ = register_phone(
        w, w.ann, code_of(w, ann, w.ann), begin={"app_version": "0.2.0"}, complete={"app_version": "0.3.1"}
    )
    assert s == 200
    assert [c["app_version"] for c in mine(w, ann)["channels"]] == ["0.2.0", "0.3.1"]
    # a bad value at complete does not replace a good one given at begin
    s, _, _ = register_phone(
        w, w.ann, code_of(w, ann, w.ann), begin={"app_version": "0.4.0"}, complete={"app_version": "<script>"}
    )
    assert s == 200
    assert mine(w, ann)["channels"][-1]["app_version"] == "0.4.0"


@pytest.mark.parametrize("bad", ["x" * 33, "1.0 beta", "<script>alert(1)</script>", 7, {"a": 1}, ["1"], "1.0\n"])
def test_a_bad_version_never_refuses_the_registration(w, bad):
    ann = w.login(w.ann)
    s, got, (signer, _) = register_phone(
        w, w.ann, code_of(w, ann, w.ann), begin={"app_version": bad}, complete={"app_version": bad}
    )
    assert s == 200, got
    (row,) = mine(w, ann)["channels"]
    assert row["app_version"] is None and row["update_needed"] is False
    assert _bind(w, signer, w.ann)  # and the phone signs in and binds as any other


@pytest.mark.parametrize(
    ("version", "old"),
    [("0.0.9", True), ("0.0.9-rc1", True), ("0.1.0", False), ("0.1.1", False), ("1.0.0", False), ("abc", False)],
)
def test_an_old_phone_is_flagged_and_still_works(w, version, old):
    assert MIN_PHONE_VERSION == "0.1.0"
    ann = w.login(w.ann)
    s, got, (signer, _) = register_phone(w, w.ann, code_of(w, ann, w.ann), begin={"app_version": version})
    assert s == 200, got
    (row,) = mine(w, ann)["channels"]
    assert row["app_version"] == version and row["update_needed"] is old
    # the flag never gates: the phone still binds a channel session and reads its own list
    ctok = _bind(w, signer, w.ann)
    assert [c["update_needed"] for c in mine(w, ctok)["channels"]] == [old]


def test_a_browser_and_a_phone_that_reports_nothing_show_no_flag(w):
    ann = w.login(w.ann)
    w.register_channel(ann)
    register_phone(w, w.ann, code_of(w, ann, w.ann))
    rows = mine(w, ann)["channels"]
    assert [(c["kind"], c["app_version"], c["update_needed"]) for c in rows] == [
        ("browser", None, False),
        ("phone", None, False),
    ]
    # a browser can never be "old": even a version on a browser row would not flag it
    assert w.api._channel_row({**rows[0], "app_version": "0.0.1"})["update_needed"] is False


def test_the_admin_list_carries_the_flag_too(w):
    ann, adm = w.login(w.ann), w.login(w.admin)
    register_phone(w, w.ann, code_of(w, ann, w.ann), begin={"app_version": "0.0.5"})
    (row,) = w.call("GET", "/admin/channels", token=adm)[1]["channels"]
    assert row["app_version"] == "0.0.5" and row["update_needed"] is True and row["user_id"] == w.ann


# --- the store -------------------------------------------------------------------------------------------------------------------


def test_an_older_channels_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = ChannelsDB(str(path))
    db.w("INSERT INTO pairing(code_hash, user_id, expires_ms) VALUES ('h','u',1)")
    for table, col in (("pairing", "source"), ("channels", "app_version"), ("challenges", "app_version")):
        db.db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    db.close()
    again = ChannelsDB(str(path))
    try:
        for table, col in (("pairing", "source"), ("channels", "app_version"), ("challenges", "app_version")):
            assert col in {r[1] for r in again.db.execute(f"PRAGMA table_info({table})")}
        assert again.q("SELECT source FROM pairing") == [("cli",)]  # an old row is an operator code
    finally:
        again.close()
