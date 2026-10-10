"""Grants: secret, hash, token format and the D60 terms as pure checks."""

from __future__ import annotations

import pytest

from orch import canon
from orch.identity import (
    Refused,
    check_grant_issued,
    check_grant_revoker,
    format_grant,
    new_grant,
    new_ulid,
    parse_grant,
    parse_timestamp,
    secret_matches,
)

AT = "2026-10-10T10:00:00Z"
HASH = "sha256:" + "ab" * 32


def ev(**over):
    base = {
        "grant": "gr_01J9ZP0000000000000000000A",
        "scope": "workable",
        "verbs": "agent",
        "issued_at": AT,
        "hours": 8,
        "expires_at": "2026-10-10T18:00:00Z",
        "secret_hash": HASH,
    }
    base.update(over)
    return base


def code(role="member", e=None, **kw):
    with pytest.raises(Refused) as x:
        check_grant_issued(e or ev(), role=role, at=kw.pop("at", AT), **kw)
    return x.value.code


def test_token_format_and_hash():
    tok, value, h = new_grant(1_790_000_000_000)
    assert value.startswith("gr_") and value.count(".") == 1 and len(tok.secret) == 32
    assert parse_grant(value).secret == tok.secret and parse_grant(value).grant_id == tok.grant_id
    assert h == canon.grant_secret_hash(tok.secret)
    assert secret_matches(tok.secret, h)
    assert not secret_matches(bytes(32), h)
    assert not secret_matches(tok.secret, "sha256:" + "0" * 64)
    assert not secret_matches(tok.secret, "junk")
    assert "redacted" in repr(tok) and tok.secret.hex() not in repr(tok)


def test_secrets_are_fresh():
    a, _, _ = new_grant()
    b, _, _ = new_grant()
    assert a.secret != b.secret and a.grant_id != b.grant_id


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "gr_01J9ZP0000000000000000000A",
        "gr_01J9ZP0000000000000000000A.short",
        "gr_01J9ZP0000000000000000000A." + "A" * 44,
        "gr_91J9ZP0000000000000000000A." + "A" * 43,
        "gr_01J9ZP0000000000000000000A." + "A" * 42 + "B",  # non-canonical trailing bits
        "GR_01J9ZP0000000000000000000A." + "A" * 43,
        None,
    ],
)
def test_bad_tokens(bad):
    with pytest.raises(Refused) as e:
        parse_grant(bad)
    assert e.value.code == "grant.token"


def test_ulid_shape_and_order():
    a, b = new_ulid(1000), new_ulid(2000)
    assert len(a) == 26 and a[0] in "01234567" and a < b
    assert new_ulid(1000) != a


def test_format_round_trip():
    tok, value, _ = new_grant()
    assert format_grant(tok) == value


def test_timestamps():
    assert parse_timestamp("1970-01-01T00:00:01Z") == 1
    for bad in ("2026-10-10 10:00:00Z", "2026-10-10T10:00:00", "2026-13-10T10:00:00Z", "2026-10-10T10:00:60Z", 5):
        with pytest.raises(Refused):
            parse_timestamp(bad)


@pytest.mark.parametrize("role", ["owner", "maintainer"])
def test_owner_and_maintainer_terms(role):
    check_grant_issued(ev(scope="all", hours=24, expires_at="2026-10-11T10:00:00Z"), role=role, at=AT)
    check_grant_issued(ev(hours=1, expires_at="2026-10-10T11:00:00Z"), role=role, at=AT)
    assert code(role, ev(hours=25, expires_at="2026-10-11T11:00:00Z")) == "grant.hours"
    assert code(role, ev(hours=0, expires_at=AT)) == "grant.hours"


def test_member_terms():
    check_grant_issued(ev(), role="member", at=AT)
    check_grant_issued(ev(hours=1, expires_at="2026-10-10T11:00:00Z"), role="member", at=AT)
    assert code("member", ev(scope="all")) == "grant.scope"
    assert code("member", ev(hours=9, expires_at="2026-10-10T19:00:00Z")) == "grant.hours"
    check_grant_issued(ev(hours=9, expires_at="2026-10-10T19:00:00Z"), role="member", at=AT, grant_hours=12)
    assert code("member", ev(hours=9, expires_at="2026-10-10T19:00:00Z"), grant_hours=8) == "grant.hours"


def test_viewer_and_unknown_roles_issue_nothing():
    assert code("viewer") == "grant.role"
    assert code("admin") == "grant.role"


def test_structure_checks():
    assert code("owner", ev(scope="everything")) == "grant.scope"
    assert code("owner", ev(hours="8")) == "grant.hours"
    assert code("owner", ev(hours=True)) == "grant.hours"
    assert code("owner", ev(expires_at="2026-10-10T18:00:01Z")) == "grant.expires_at"
    assert code("owner", ev(issued_at="2026-10-10T10:05:01Z", expires_at="2026-10-10T18:05:01Z")) == "grant.issued_at"
    check_grant_issued(ev(issued_at="2026-10-10T10:05:00Z", expires_at="2026-10-10T18:05:00Z"), role="owner", at=AT)
    assert code("owner", ev(verbs="all")) == "grant.verbs"
    assert code("owner", ev(verbs=[])) == "grant.verbs"
    assert code("owner", ev(verbs=["a", "a"])) == "grant.verbs"
    assert code("owner", ev(verbs=["claim", "approve"]), human_only={"approve"}) == "grant.verbs"
    check_grant_issued(ev(verbs=["claim"]), role="owner", at=AT, human_only={"approve"})
    assert code("owner", ev(secret_hash="nope")) == "grant.secret_hash"
    assert code("owner", grant_hours=0) == "grant.hours"


def test_revocation_terms():
    check_grant_revoker(revoker_role="owner", revoker="p_a", issuer="p_b")
    check_grant_revoker(revoker_role="maintainer", revoker="p_a", issuer="p_a")
    check_grant_revoker(revoker_role="member", revoker="p_a", issuer="p_a")
    for role in ("maintainer", "member"):
        with pytest.raises(Refused) as e:
            check_grant_revoker(revoker_role=role, revoker="p_a", issuer="p_b")
        assert e.value.code == "grant.revoke_not_own"
    with pytest.raises(Refused) as e:
        check_grant_revoker(revoker_role="viewer", revoker="p_a", issuer="p_a")
    assert e.value.code == "grant.revoke_role"
