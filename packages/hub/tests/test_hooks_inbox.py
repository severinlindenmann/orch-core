import base64
import hashlib
import hmac

from orch_hub.contract import hooks as HK
from orch_hub.hooks import inbox as IB
from orch_hub.hooks.store import HookStore

NOW = 1_800_000_000_000
WS = "a" * 32
TOKEN = "hh_" + "T" * 43
SECRET = "hs_" + "S" * 43
BODY = b'{"x":1}'


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def setup(source="github", limits=None):
    clk = Clock()
    st = HookStore()
    st.put(
        WS,
        {
            "token_hash": HK.token_hash(TOKEN),
            "followup": "FU-1",
            "source": source,
            "secret": SECRET,
            "expires_ms": NOW + 3_600_000,
        },
        NOW,
    )
    return clk, st, IB.HookInbox(st, clk, "https://hub.example/h/", limits)


def ring(ib, headers, body=BODY, token=TOKEN, src="1.2.3.4"):
    if not ib.admit(src):
        return IB.TOO_MANY
    hook = ib.find(token, src)
    if hook is None:
        return IB.NOT_FOUND
    return ib.finish(hook, headers, body, src)


def gh(body=BODY, secret=SECRET):
    return {"x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def test_github_ring_is_stored_once():
    clk, st, ib = setup()
    assert ring(ib, gh()) == 204
    assert ring(ib, gh()) == 204  # a second ring: same answer, nothing new
    r = st.pull(WS, 0, clk())
    assert len(r["rings"]) == 1 and r["rings"][0]["received_at"] == NOW


def test_bad_signature_unknown_and_missing_are_one_answer():
    clk, st, ib = setup()
    answers = {
        ring(ib, gh(secret="hs_" + "X" * 43)),
        ring(ib, {}),
        ring(ib, gh(), token="hh_" + "U" * 43),
        ring(ib, gh(), token="nonsense"),
        ring(ib, {"x-hub-signature-256": "sha256=zz"}),
    }
    assert answers == {404}
    assert st.pull(WS, 0, clk())["rings"] == []


def test_generic_window_and_replay_bound():
    _clk, _st, ib = setup("generic")
    t = NOW // 1000
    m = HK.generic_mac(SECRET, t)
    m.update(BODY)
    assert ring(ib, {"x-orch-signature": f"t={t},v1={m.hexdigest()}"}) == 204
    clk2, _st2, ib2 = setup("generic")
    clk2.t = NOW + 301_000  # the same signed request, five minutes and a second later
    assert ring(ib2, {"x-orch-signature": f"t={t},v1={m.hexdigest()}"}) == 404


def test_databricks_basic_needs_both_fields():
    _c, st, ib = setup("databricks")

    def basic(user, pw):
        return {"authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    assert ring(ib, basic("admin", SECRET)) == 404
    assert ring(ib, basic("orch", "hs_" + "x" * 43)) == 404
    assert ring(ib, {"authorization": "Bearer " + SECRET}) == 404
    assert ring(ib, basic("orch", SECRET)) == 204
    assert len(st.pull(WS, 0, NOW)["rings"]) == 1


def test_per_hook_bucket_only_after_the_signature_and_is_silent():
    _clk, _st, ib = setup(limits=IB.Limits(ip_per_min=600, ip_burst=600, hook_per_min=2, hook_burst=2))
    # an outsider (bad signature) never learns the bucket: still 404, however many times
    assert {ring(ib, gh(secret="hs_" + "X" * 43), src="9.9.9.9") for _ in range(5)} == {404}
    codes = [ring(ib, gh(), src=f"10.0.0.{i}") for i in range(5)]
    assert codes == [204] * 5  # over the hook limit: still 204, silently


def test_miss_bucket_then_429_before_looking_at_the_token():
    _c, _s, ib = setup(limits=IB.Limits(miss_per_min=3, miss_burst=3))
    codes = [ring(ib, gh(), token="hh_" + "U" * 43) for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]
    assert ring(ib, gh(), src="5.6.7.8") == 204  # another sender is unaffected


def test_ip_bucket_and_global_bucket():
    _c, _s, ib = setup(limits=IB.Limits(ip_per_min=60, ip_burst=2))
    assert [ring(ib, gh()) for _ in range(3)] == [204, 204, 429]
    _c, _s, ib = setup(limits=IB.Limits(global_per_min=120))  # 2 a second
    assert sum(ring(ib, gh(), src=f"1.1.1.{i}") == 429 for i in range(10)) == 8


def test_expired_and_revoked_are_404():
    clk, st, ib = setup()
    clk.t = NOW + 3_600_000
    assert ring(ib, gh()) == 404
    clk.t = NOW
    st.revoke(WS, HK.token_hash(TOKEN))
    assert ring(ib, gh()) == 404


def test_disabled_without_public_url():
    _c, _s, ib = setup()
    ib.url_base = None
    assert not ib.enabled


def test_github_ping_is_answered_but_does_not_use_the_ring():
    clk, st, ib = setup()
    assert ring(ib, {**gh(), "x-github-event": "ping"}) == 204
    assert st.pull(WS, 0, clk())["rings"] == []
    assert ring(ib, {**gh(), "x-github-event": "check_suite"}) == 204  # the real event still rings
    assert len(st.pull(WS, 0, clk())["rings"]) == 1


def test_a_ping_without_a_good_signature_is_no_exception():
    _clk, _st, ib = setup()
    assert ring(ib, {"x-github-event": "ping"}) == 404
    assert ring(ib, {**gh(secret="hs_" + "X" * 43), "x-github-event": "ping"}) == 404


def test_ping_header_means_nothing_for_other_sources():
    clk, st, ib = setup("generic")
    t = NOW // 1000
    m = HK.generic_mac(SECRET, t)
    m.update(BODY)
    assert ring(ib, {"x-orch-signature": f"t={t},v1={m.hexdigest()}", "x-github-event": "ping"}) == 204
    assert len(st.pull(WS, 0, clk())["rings"]) == 1
