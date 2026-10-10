"""``orch grant`` and ``orch grant revoke``: D60 terms, and a secret that is shown once and kept nowhere."""

from __future__ import annotations

import json
import re

from orch import canon, crypto
from orch.identity import parse_grant
from tests.ops.helpers import Cli

SECRET = re.compile(r"ORCH_GRANT=(gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43})")


def issued(ws):
    return [e for e in ws.read_events("workspace") if e["type"] == "grant.issued"]


def shown_value(ws):
    (text,) = ws.shown_secrets
    return SECRET.search(text).group(1)


def test_the_default_grant_has_the_d60_terms_and_the_secret_is_shown_once(hws, me):
    n = len(issued(hws))
    r = me("grant", "--label", "laptop")
    assert r.code == 0, r.err
    e = issued(hws)[n]
    assert (e["scope"], e["verbs"], e["hours"], e["label"]) == ("workable", "agent", 8, "laptop")
    assert e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase" and "sig" in e
    assert e["expires_at"] > e["issued_at"]
    assert r.first.startswith(f"ok grant.issued {e['grant']} until={e['expires_at']}")
    value = shown_value(hws)  # shown on the terminal: the seam stands for /dev/tty
    token = parse_grant(value)
    assert token.grant_id == e["grant"] and e["secret_hash"] == canon.grant_secret_hash(token.secret)
    assert (
        value not in r.out and value not in r.err and token.grant_id in r.out and crypto.b64u(token.secret) not in r.out
    )


def test_the_grant_works_for_an_agent_and_nothing_stores_the_secret(hws, agent, me, tmp_path):
    r = me("grant", "--json")
    assert r.code == 0 and "secret" not in json.dumps(r.doc)
    value = shown_value(hws)
    secret = value.partition(".")[2]
    use = Cli(hws, grant=False)
    s = use("status", ORCH_GRANT=value)
    assert s.code == 0, s.err
    assert secret not in s.out and secret not in s.err
    for p in list(tmp_path.rglob("*")):  # workspace, host state, session records, key files: nowhere on disk
        if p.is_file():
            data = p.read_bytes()
            assert secret.encode() not in data, p
            assert value.encode() not in data, p
    for stream in (r.out, r.err):
        assert secret not in stream


def test_a_second_run_is_a_second_grant_never_a_replay(hws, me):
    assert me("grant").code == 0 and me("grant").code == 0
    a, b = issued(hws)[-2:]
    assert a["grant"] != b["grant"] and a["secret_hash"] != b["secret_hash"]
    assert len(hws.shown_secrets) == 2 and hws.shown_secrets[0] != hws.shown_secrets[1]


def test_verbs_scope_and_hours(hws, me):
    r = me("grant", "--hours", "2", "--scope", "all", "--verbs", "task.done,log")
    assert r.code == 0, r.err
    e = issued(hws)[-1]
    assert (e["hours"], e["scope"], e["verbs"]) == (2, "all", ["task.done", "log"])
    assert me("grant", "--hours", "0", "--json").err_code == "invalid.input"
    assert me("grant", "--hours", "25", "--json").err_code == "invalid.input"
    assert me("grant", "--verbs", "approve", "--json").err_code == "invalid.input"  # a human operation is never granted
    assert me("grant", "--verbs", "task", "--json").err_code == "invalid.input"  # exact operation names only
    assert me("grant", "--verbs", "Bad Name", "--json").err_code == "invalid.input"
    assert len(hws.shown_secrets) == 1  # the refusals showed no secret


def test_a_member_gets_workable_grants_up_to_the_workspace_hours(hws, me):
    hws.act_as(hws.add_member("mia", "member"))
    assert me("grant", "--scope", "all", "--json").err_code == "invalid.input"
    assert me("grant", "--hours", "9", "--json").err_code == "invalid.input"
    assert me("grant", "--hours", "8").code == 0
    assert issued(hws)[-1]["actor"]["id"] == hws.people["mia"].ref


def test_a_viewer_gets_no_grant_and_is_not_asked_for_a_passphrase(hws, me):
    hws.act_as(hws.add_member("vera", "viewer"))
    hws.provider.requests.clear()
    r = me("grant", "--json")
    assert r.code == 3 and r.err_code == "role.denied"
    assert hws.provider.requests == [] and hws.shown_secrets == []


def test_revoke_ends_the_grant_and_every_session_using_it(hws, me):
    assert me("grant").code == 0
    value = shown_value(hws)
    gid = parse_grant(value).grant_id
    use = Cli(hws, grant=False)
    assert use("status", ORCH_GRANT=value).code == 0
    r = me("grant", "revoke", gid, "--reason", "laptop lost")
    assert r.code == 0, r.err
    assert r.first.startswith(f"ok grant.revoked {gid} seq=")
    ev = [e for e in hws.read_events("workspace") if e["type"] == "grant.revoked"][-1]
    assert ev["grant"] == gid and ev["reason"] == "laptop lost" and ev["auth"] == "passphrase"
    r = use("new", "x", "--json", ORCH_GRANT=value)
    assert r.code == 3 and r.err_code == "grant.expired"


def test_revoke_of_an_unknown_grant_and_the_roles(hws, me):
    assert me("grant", "revoke", "gr_01J9ZP0000000000000000000A", "--json").err_code == "not_found"
    assert me("grant", "--label", "owner's").code == 0
    gid = parse_grant(shown_value(hws)).grant_id
    hws.act_as(hws.add_member("mia", "maintainer"))
    r = me("grant", "revoke", gid, "--json")  # a maintainer revokes only their own
    assert r.code == 3 and r.err_code == "role.denied", r.out
    assert me("grant").code == 0
    mine = parse_grant(SECRET.search(hws.shown_secrets[-1]).group(1)).grant_id
    assert me("grant", "revoke", mine).code == 0


def test_a_secret_that_cannot_be_shown_is_said_and_the_grant_can_be_revoked(hws, me, monkeypatch):
    from orch.custody import NoPrompt
    from orch.ops import human

    def nowhere(_text):
        raise NoPrompt("no terminal")

    monkeypatch.setattr(human, "show_secret", nowhere)
    r = me("grant", "--json")
    gid = issued(hws)[-1]["grant"]
    assert r.code == 3 and r.err_code == "custody.no_prompt" and gid in r.doc["error"]["message"]
    assert me("grant", "revoke", gid).code == 0
