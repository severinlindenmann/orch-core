import base64
import hashlib
import hmac
import json

import pytest
from joserfc.jwk import ECKey
from oidc_fake_idp import CLIENT, NOW, FakeIdp, entra_cfg, google_cfg, verifier_ok

from orch_hub.oidc import Oidc, OidcConfig, OidcError, mfa_ok

KEY = b"k" * 32


class T(Oidc):
    """Test wrapper: a default source, the fake IdP's host."""

    def start(self, *, fresh=False, source="t"):
        return super().start(fresh=fresh, source=source)

    def finish(self, cookie, state, code, secret, source="t"):
        return super().finish(cookie, state, code, secret, source)


def make(cfg=None, clock=None):
    cfg = cfg or entra_cfg()
    idp = FakeIdp(cfg)
    t = clock or [NOW]
    return T(cfg, idp, KEY, lambda: t[0], frozenset({"idp.example"})), idp, t


def login(o, idp, **kw):
    url, cookie = o.start(fresh=kw.pop("fresh", False))
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    return o.finish(cookie, q["state"], "code1", "SECRET"), q, cookie


def refused(o, idp, code, **kw):
    with pytest.raises(OidcError) as e:
        login(o, idp, **kw)
    assert e.value.code == code


def test_happy_entra_pkce_and_identity():
    o, idp, _ = make()
    ident, q, _ = login(o, idp)
    assert (ident.provider, ident.realm, ident.subject) == ("entra", entra_cfg().tenant, "oid-1")
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == entra_cfg().redirect_uri
    chal = base64.urlsafe_b64encode(hashlib.sha256(idp.last_form["code_verifier"].encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == chal.decode()  # PKCE: the verifier we sent matches the challenge
    assert idp.last_form["client_secret"] == "SECRET" and "SECRET" not in repr(ident)


def test_happy_google():
    o, idp, _ = make(google_cfg())
    ident, _, _ = login(o, idp)
    assert (ident.realm, ident.subject) == ("acme.example", "sub-1")


def test_email_is_not_identity():
    o, idp, _ = make()
    idp.claims = {"email": "someone.else@acme.example"}
    ident, _, _ = login(o, idp)
    assert ident.subject == "oid-1"  # same person key whatever the email says


@pytest.mark.parametrize(
    "claims,code",
    [
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "bad_issuer"),
        ({"aud": "other"}, "bad_audience"),
        ({"aud": [CLIENT, "other"]}, "bad_audience"),
        ({"aud": [CLIENT]}, "bad_audience"),
        ({"azp": "other"}, "bad_audience"),
        ({"exp": NOW - 500}, "expired"),
        ({"iat": NOW + 500}, "expired"),
        ({"nbf": NOW + 500}, "expired"),
        ({"nonce": "wrong"}, "bad_nonce"),
        ({"tid": "99999999-2222-3333-4444-555555555555"}, "wrong_tenant"),
    ],
)
def test_claim_refusals(claims, code):
    o, idp, _ = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    idp.claims = claims
    with pytest.raises(OidcError) as e:
        o.finish(cookie, q["state"], "c", "S")
    assert e.value.code == code


def test_skew_allowed():
    o, idp, _ = make()
    idp.claims = {"exp": NOW - 60}
    login(o, idp)


def test_google_refusals():
    o, idp, _ = make(google_cfg())
    idp.claims = {"email_verified": False}
    refused(o, idp, "email_unverified")
    o, idp, _ = make(google_cfg())
    idp.claims = {"hd": "evil.example"}
    refused(o, idp, "wrong_domain")
    o, idp, _ = make(google_cfg())
    idp.claims = {"iss": "accounts.google.com"}
    login(o, idp)  # the bare form is allowed for google
    o, idp, _ = make(google_cfg())
    idp.drop = {"hd"}
    refused(o, idp, "wrong_domain")


def test_entra_needs_oid():
    o, idp, _ = make()
    idp.drop = {"oid"}
    refused(o, idp, "bad_token")


def test_bad_signatures_and_algs():
    o, idp, _ = make()
    idp.signer = ECKey.generate_key("P-256", {"kid": "k1"})  # wrong key, same kid
    idp.alg = "ES256"
    refused(o, idp, "bad_token")
    # RSA public key used as an HMAC secret (HS256 confusion)
    o, idp, _ = make()
    url, _cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    signing = b(b'{"alg":"HS256","kid":"k1"}') + "." + b(json.dumps(idp.base_claims()).encode())
    mac = hmac.new(idp.key.as_pem(private=False), signing.encode(), hashlib.sha256).digest()
    with pytest.raises(OidcError) as e:
        o.verify(signing + "." + b(mac), q["nonce"])
    assert e.value.code == "bad_token"
    # alg none
    o, idp, _ = make()
    url, _cookie = o.start()
    q = verifier_ok(idp, url)
    head = base64.urlsafe_b64encode(b'{"alg":"none","kid":"k1"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b'{"iss":"x"}').rstrip(b"=").decode()
    with pytest.raises(OidcError):
        o.verify(f"{head}.{body}.", q["nonce"])


def test_jku_x5u_ignored():
    o, idp, _ = make()
    idp.header_extra = {"jku": "https://evil.example/jwks", "x5u": "https://evil.example/c"}
    login(o, idp)
    assert idp.jwks_calls == 1  # only the discovered jwks_uri was ever fetched


def test_jwks_failure_fails_closed():
    o, idp, _ = make()
    idp.jwks_fails = True
    refused(o, idp, "jwks_unavailable")


def test_unknown_kid_one_rate_limited_refetch_and_rotation():
    o, idp, t = make()
    login(o, idp)
    assert idp.jwks_calls == 1
    new = type(idp.key).generate_key(2048, {"kid": "k2", "use": "sig"})
    idp.key, idp.extra = new, []  # rotate: signer is k2, JWKS now only k2
    refused(o, idp, "unknown_kid")  # inside the window: no refetch
    assert idp.jwks_calls == 1
    t[0] += 61
    login(o, idp)  # outside the window: one refetch, rotation works
    assert idp.jwks_calls == 2


def test_state_mismatch_refused_and_burns_nothing():
    o, idp, _ = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    with pytest.raises(OidcError):
        o.finish(cookie, "wrong-state", "c", "S")
    o.finish(cookie, q["state"], "c", "S")  # the real callback still works


def test_replay_refused():
    o, idp, _ = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    o.finish(cookie, q["state"], "c", "S")
    with pytest.raises(OidcError) as e:
        o.finish(cookie, q["state"], "c", "S")
    assert e.value.code == "bad_state"


def test_tampered_or_foreign_cookie_refused():
    o, idp, _ = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    flipped = cookie[:-2] + ("AA" if cookie[-2:] != "AA" else "BB")
    for bad in (flipped, "", "junk", cookie[:10]):
        with pytest.raises(OidcError):
            o.finish(bad, q["state"], "c", "S")
    other = T(
        entra_cfg(), idp, b"z" * 32, lambda: NOW, frozenset({"idp.example"})
    )  # a cookie sealed under another hub key
    with pytest.raises(OidcError):
        other.finish(cookie, q["state"], "c", "S")


def test_thousands_of_starts_never_block_a_login():
    o, idp, _ = make()
    for i in range(1500):  # from many sources (one source is rate limited)
        o.start(source=f"10.0.{i // 250}.{i % 250}")
    ident, _, _ = login(o, idp)
    assert ident.subject == "oid-1"
    assert not hasattr(o, "_tx")  # no per-start server state


def test_tx_expires():
    o, idp, t = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    t[0] += 700
    with pytest.raises(OidcError):
        o.finish(cookie, q["state"], "c", "S")


def test_fresh_login():
    o, idp, _ = make()
    refused(o, idp, "not_fresh", fresh=True)  # no auth_time
    o, idp, _ = make()
    idp.claims = {"auth_time": NOW - 1000}
    refused(o, idp, "not_fresh", fresh=True)
    o, idp, _ = make()
    idp.claims = {"auth_time": NOW - 10}
    ident, q, _ = login(o, idp, fresh=True)
    assert ident.auth_time == NOW - 10 and q["prompt"] == "login" and q["max_age"] == "0"


def test_mfa_statement():
    o, idp, _ = make()
    assert mfa_ok(login(o, idp)[0])  # no amr: the admin's statement stands
    o, idp, _ = make()
    idp.claims = {"amr": ["pwd"]}
    assert not mfa_ok(login(o, idp)[0])
    o, idp, _ = make()
    idp.claims = {"amr": ["pwd", "mfa"]}
    assert mfa_ok(login(o, idp)[0])


def test_config_is_strict():
    for kw in (
        {"tenant": "common"},
        {"tenant": "organizations"},
        {"redirect_uri": "http://192.168.1.5:8100/cb"},
        {"redirect_uri": "http://hub.example/cb"},
        {"client_id": ""},
    ):
        base = {
            "provider": "entra",
            "client_id": "c",
            "redirect_uri": "https://hub.example/oidc/callback",
            "tenant": entra_cfg().tenant,
        }
        base.update(kw)
        with pytest.raises(OidcError):
            OidcConfig(**base)
    OidcConfig("entra", "c", "http://127.0.0.1:8100/cb", tenant=entra_cfg().tenant)
    with pytest.raises(OidcError):
        OidcConfig("google", "c", "https://h.example/cb", domain="")


def test_discovery_must_name_pinned_issuer():
    o, idp, _ = make()
    orig = idp.get_json
    idp.get_json = lambda u: (
        {**orig(u), "issuer": "https://login.microsoftonline.com/common/v2.0"}
        if u.endswith("configuration")
        else orig(u)
    )
    with pytest.raises(OidcError):
        o.start()


def test_replay_of_a_real_cookie_after_junk_still_refused():
    o, idp, _ = make()
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    o.finish(cookie, q["state"], "c", "S")
    for i in range(50):
        _u2, c2 = o.start(source=f"s{i}")
        with pytest.raises(OidcError):
            o.finish(c2, "wrong", "c", "S")
    with pytest.raises(OidcError):
        o.finish(cookie, q["state"], "c", "S")


def test_cookie_length_cap_and_config_binding():
    o, idp, _ = make()
    with pytest.raises(OidcError):
        o.finish("A" * 5000, "s", "c", "S")
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    changed = T(
        OidcConfig("entra", "other-client", entra_cfg().redirect_uri, tenant=entra_cfg().tenant), idp, KEY, lambda: NOW
    )
    with pytest.raises(OidcError):
        changed.finish(cookie, q["state"], "c", "S")
    tenant2 = T(
        entra_cfg(tenant="99999999-2222-3333-4444-555555555555"), idp, KEY, lambda: NOW, frozenset({"idp.example"})
    )
    with pytest.raises(OidcError):
        tenant2.finish(cookie, q["state"], "c", "S")


def burnable(o, idp):
    url, cookie = o.start()
    q = verifier_ok(idp, url)
    idp.nonce = q["nonce"]
    return cookie, q["state"]


def test_two_encodings_of_one_cookie_second_refused():
    import base64 as b64

    o, idp, _ = make()
    cookie, state = burnable(o, idp)
    o.finish(cookie, state, "c", "S")
    raw = b64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4))
    for variant in (cookie + "=", cookie + "==", b64.b64encode(raw).decode(), b64.urlsafe_b64encode(raw).decode()):
        with pytest.raises(OidcError):
            o.finish(variant, state, "c", "S")
    assert len(o._done) == 1


def test_start_rate_limit_per_source():
    o, _idp, t = make()
    for _ in range(10):
        o.start(source="1.1.1.1")
    with pytest.raises(OidcError) as e:
        o.start(source="1.1.1.1")
    assert e.value.code == "rate_limited"
    o.start(source="9.9.9.9")  # another source is not affected
    t[0] += 7
    o.start(source="1.1.1.1")  # refilled


def test_parallel_finishes_of_one_cookie_only_one_passes():
    import threading

    o, idp, _ = make()
    cookie, state = burnable(o, idp)
    results = []
    gate = threading.Barrier(8)

    def go():
        gate.wait()
        try:
            o.finish(cookie, state, "c", "S")
            results.append("ok")
        except OidcError:
            results.append("refused")

    ts = [threading.Thread(target=go) for _ in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert results.count("ok") == 1 and results.count("refused") == 7


def test_failed_and_forged_finishes_use_no_capacity_and_do_not_block(monkeypatch):
    import orch_hub.oidc as M

    monkeypatch.setattr(M, "FINISH_BURST", 10**9)
    monkeypatch.setattr(M, "USED_MAX", 5)
    o, idp, _ = make()
    o._finish_lim = M._Limiter(10**9, 1.0)
    cookie, state = burnable(o, idp)
    idp.post_form = lambda url, data: {"nothing": 1}  # the IdP refuses the code
    for i in range(2000):
        with pytest.raises(OidcError):
            o.finish(cookie if i % 2 else f"forged{i}", state, "junk", "S")
    assert not o._done and not o._inflight
    del idp.post_form
    idp.nonce = verifier_ok(idp, o.start()[0])["nonce"]  # unrelated, a real login follows
    c2, s2 = burnable(o, idp)
    o.finish(c2, s2, "c", "S")  # the real login still works after 2000 failures


def test_same_nonce_completing_twice_second_refused():
    o, idp, _ = make()
    cookie, state = burnable(o, idp)
    o.finish(cookie, state, "c", "S")
    with pytest.raises(OidcError) as e:
        o.finish(cookie, state, "c", "S")
    assert e.value.code == "bad_state"


def test_full_done_set_evicts_oldest_and_never_refuses(monkeypatch):
    import orch_hub.oidc as M

    monkeypatch.setattr(M, "USED_MAX", 3)
    o, idp, _ = make()
    for _ in range(6):
        c, st = burnable(o, idp)
        o.finish(c, st, "c", "S")
    assert len(o._done) == 3


def test_finish_rate_limit_per_source_and_ipv6_slash64():
    import orch_hub.oidc as M

    o, idp, _ = make()
    cookie, state = burnable(o, idp)
    for _ in range(10):
        with pytest.raises(OidcError):
            o.finish("junk", state, "c", "S", source="2001:db8:1:2::1")
    with pytest.raises(OidcError) as e:  # another address in the same /64 shares the bucket
        o.finish(cookie, state, "c", "S", source="2001:db8:1:2:ffff::9")
    assert e.value.code == "rate_limited"
    o.finish(cookie, state, "c", "S", source="2001:db8:1:3::1")  # another /64 is fine
    assert M.source_key("2001:db8:1:2::1") == M.source_key("2001:db8:1:2:aaaa::1")
    assert M.source_key("10.1.2.3") == "10.1.2.3"
    for _ in range(10):
        o.start(source="2001:db8:9:9::1")
    with pytest.raises(OidcError):
        o.start(source="2001:db8:9:9::2")


def test_limiter_lru_is_bounded():
    import orch_hub.oidc as M

    lim = M._Limiter(1, 1.0, cap=100)
    for i in range(1000):
        lim.allow(f"10.0.{i // 250}.{i % 250}", 0.0)
    assert len(lim._m) == 100


def test_ipv4_mapped_peer_is_its_ipv4_bucket():
    import orch_hub.oidc as M

    assert M.source_key("::ffff:1.2.3.4") == M.source_key("1.2.3.4") == "1.2.3.4"
    assert M.source_key("::ffff:1.2.3.4") != M.source_key("::ffff:5.6.7.8")  # not one shared /64


def test_empty_source_is_a_bug():
    o, _idp, _ = make()
    with pytest.raises(OidcError) as e:
        Oidc.start(o, source="")
    assert e.value.code == "bad_config"
    with pytest.raises(OidcError):
        Oidc.finish(o, "x", "s", "c", "S", source="")


def test_endpoint_hosts_must_be_the_providers_own():
    for k, evil in (
        ("token_endpoint", "https://evil.example/token"),
        ("jwks_uri", "http://idp.example/jwks"),
        ("authorization_endpoint", "https://idp.example:8443/a"),
    ):
        o, idp, _ = make()
        orig = idp.get_json
        idp.get_json = lambda u, orig=orig, k=k, evil=evil: (
            {**orig(u), k: evil} if u.endswith("configuration") else orig(u)
        )
        with pytest.raises(OidcError) as e:
            o.start()
        assert e.value.code == "bad_discovery"
    real = Oidc(entra_cfg(), FakeIdp(entra_cfg()), KEY)  # the real provider host list
    assert real._hosts == frozenset({"login.microsoftonline.com"})
    with pytest.raises(OidcError):  # the fake IdP's host is not Microsoft's
        real.start(source="t")


def test_outage_fetches_happen_outside_the_lock_and_are_negative_cached():
    import threading

    o, idp, t = make()
    idp.jwks_fails = True
    inside = []
    orig = idp.get_json

    def spy(u):
        inside.append(o._lock.acquire(blocking=False))  # True = the lock was free during the fetch
        if inside[-1]:
            o._lock.release()
        return orig(u)

    idp.get_json = spy
    refused(o, idp, "jwks_unavailable")
    assert all(inside)
    n = idp.jwks_calls
    for _ in range(5):  # inside the window: no new fetch
        refused(o, idp, "jwks_unavailable")
    assert idp.jwks_calls == n
    t[0] += 6
    idp.jwks_fails = False
    login(o, idp)  # recovers after the window
    # discovery failure is cached too
    o2, idp2, _t2 = make()
    calls = []
    idp2.get_json = lambda u: calls.append(u) or (_ for _ in ()).throw(OSError("down"))
    for _ in range(4):
        with pytest.raises(OidcError):
            o2.start()
    assert len(calls) == 1
    assert threading.active_count() >= 1
