"""R1 release-gate adversarial tests (11A.10 row R1 and the R1 parts of the transport row), against a stateful
simulated relay (``sim.py``) on loopback: origin binding across origins, kind and workspace in ``auth_bytes``, a
consumed challenge replayed, single-flight re-login, first link interrupted after each step, ``fenced``
mid-publication, a second host instance taking over, and log redaction over whole flows."""

from __future__ import annotations

import logging
import threading

import pytest

from orch import canon, crypto
from orch.net import http
from orch.relay import retry
from orch.relay.link import LinkRefused, first_link
from orch.relay.session import Fenced, LoginHeldOff, OriginMismatch, RelayMisbehaving
from orch.wsproto import wire

from . import corpus
from .helpers import GENESIS, GENESIS_CERT, GENESIS_PIN, OWNER_PK, WS, Alarms, Clock, Holder, Key, directory, make_card
from .helpers import retrier as make_retrier
from .helpers import session as make_session
from .mock import MockRelay
from .sim import SimRelay


@pytest.fixture
def env(tmp_path):
    from orch.wsproto.state import StateDir

    m = MockRelay()
    m.store = StateDir(tmp_path / "relay")
    sim = SimRelay(m.origin(), owner_pk=OWNER_PK)
    m.fallback = sim
    yield m, sim
    assert m.errors == []
    m.close()


def seed_card(m, sim):
    sim.cards[WS] = make_card(m.origin())


def new(m, sim, link_origin=None, **kw):
    clock = kw.pop("clock", None) or Clock(sim)
    return make_session(m.origin(), link_origin or m.origin(), clock=clock, **kw), clock


# --- origin binding -------------------------------------------------------------------------------------------------


def test_login_against_the_stored_origin(env):
    m, sim = env
    seed_card(m, sim)
    s, _ = new(m, sim)
    s.login()
    assert sim.logins == 1


@pytest.mark.parametrize("other", ["http://localhost:{port}", "http://127.0.0.1:1", "https://relay.example"])
def test_wrong_origin_is_bad_signature_then_holdoff_then_alarm(env, other):
    m, sim = env
    seed_card(m, sim)
    alarms = Alarms()
    s, clock = new(m, sim, other.format(port=m.port), alarms=alarms)
    with pytest.raises(retry.RelayRefused) as e:
        s.login()
    assert e.value.code == "bad_signature"
    n = len(m.seen)
    with pytest.raises(LoginHeldOff):
        s.login()  # within 60 s: not retried, nothing sent
    assert len(m.seen) == n
    clock.advance(61)
    with pytest.raises(retry.RelayRefused):
        s.login()
    clock.advance(61)
    with pytest.raises(OriginMismatch):
        s.login()
    assert alarms.codes() == ["origin_mismatch_suspected"]
    assert other.format(port=m.port) in alarms[0][1]  # the alarm names the stored origin
    assert sim.logins == 0


def test_bad_signature_counter_resets_after_a_success(env):
    m, sim = env
    seed_card(m, sim)
    alarms = Alarms()
    s, clock = new(m, sim, alarms=alarms)
    for _ in range(2):
        sim.origin = "https://elsewhere.example"
        with pytest.raises(retry.RelayRefused):
            s.login()
        clock.advance(61)
    sim.origin = m.origin()
    s.login()
    sim.origin = "https://elsewhere.example"
    clock.advance(1)
    with pytest.raises(retry.RelayRefused):
        s.login()
    assert alarms == []


def test_signature_for_one_origin_is_refused_by_a_relay_with_another(env):
    """A login captured by relay A (origin A) and replayed to relay B with its own challenge state never verifies."""
    m, sim = env
    seed_card(m, sim)
    key = Key()
    ch = bytes(range(32))
    sig_a = crypto.sign(key.priv, wire.auth_bytes("https://relay-a.example", ch, "workspace", WS))
    sim.challenges[ch] = sim.now + 60_000
    body = canon.dumps({"challenge": crypto.b64u(ch), "kind": "workspace", "id": WS, "sig": crypto.b64u(sig_a)})
    r = http.RelayConn(m.origin()).request(
        "POST", "/v2/session/login", route="R", headers={"Content-Type": "application/json"}, body=body,
        deadline_s=5, cap=16384,
    )  # fmt: skip
    assert r.status == 401 and b"bad_signature" in r.body


@pytest.mark.parametrize("kind,ident", [("device", WS), ("workspace", "00" * 16)])
def test_kind_and_workspace_are_bound(env, kind, ident):
    m, sim = env
    seed_card(m, sim)
    ch = bytes(range(32))
    sig = crypto.sign(Key().priv, wire.auth_bytes(m.origin(), ch, "workspace", WS))
    sim.challenges[ch] = sim.now + 60_000
    body = canon.dumps({"challenge": crypto.b64u(ch), "kind": kind, "id": ident, "sig": crypto.b64u(sig)})
    r = http.RelayConn(m.origin()).request(
        "POST", "/v2/session/login", route="R", headers={"Content-Type": "application/json"}, body=body,
        deadline_s=5, cap=16384,
    )  # fmt: skip
    assert r.status == 401


# --- challenges -----------------------------------------------------------------------------------------------------


def test_consumed_challenge_replayed_is_refused(env):
    m, sim = env
    seed_card(m, sim)
    s, _ = new(m, sim)
    s.login()
    captured = sim.login_bodies[-1]
    r = http.RelayConn(m.origin()).request(
        "POST", "/v2/session/login", route="R", headers={"Content-Type": "application/json"}, body=captured,
        deadline_s=5, cap=16384,
    )  # fmt: skip
    assert r.status == 401 and b"unknown_challenge" in r.body
    assert sim.logins == 1


def test_every_login_draws_a_fresh_challenge(env):
    m, sim = env
    seed_card(m, sim)
    s, _ = new(m, sim)
    for _ in range(3):
        s.login()
    assert len(set(sim.issued)) == 3 == len(sim.issued)
    sent = [canon.loads_strict(b)["challenge"] for b in sim.login_bodies]
    assert len(set(sent)) == 3


def test_a_relay_serving_one_challenge_twice_gets_one_signature(env):
    m, sim = env
    seed_card(m, sim)
    key = Key()
    s, _ = new(m, sim, key=key)
    sim.repeat_challenge = bytes(range(32))
    s.login()
    signed = key.count
    with pytest.raises(RelayMisbehaving):
        s.login()
    assert key.count == signed


def test_expired_challenge_then_fresh_login(env):
    m, sim = env
    seed_card(m, sim)
    s, clock = new(m, sim)
    orig = sim._challenge

    def slow_challenge():
        r = orig()
        sim.now += 61_000  # the relay's clock passes the challenge's 60 s before the login arrives
        return r

    sim._challenge = slow_challenge
    with pytest.raises(retry.RelayRefused) as e:
        s.login()
    assert e.value.code == "expired"
    sim._challenge = orig
    s.login()
    assert sim.logins == 1


# --- re-login -------------------------------------------------------------------------------------------------------


def test_relogin_before_expiry_on_the_monotonic_clock(env):
    m, sim = env
    seed_card(m, sim)
    s, clock = new(m, sim)
    d = directory(s)
    assert d.get_card() is not None and sim.logins == 1
    clock.t += 779  # the host's clock only; the relay's tokens are still valid
    d.get_card()
    assert sim.logins == 1
    clock.t += 2
    d.get_card()
    assert sim.logins == 2


def test_relay_expires_ms_is_not_trusted(env):
    """A relay announcing a token valid for a year does not stretch the re-login schedule."""
    m, sim = env
    seed_card(m, sim)
    orig = sim._login

    def long_lived(seen):
        r = orig(seen)
        if r.status == 200:
            o = canon.loads_strict(r.body)
            o["expires_ms"] = sim.now + 365 * 86_400_000
            r.body = canon.dumps(o)
        return r

    sim._login = long_lived
    s, clock = new(m, sim)
    directory(s).get_card()
    clock.t += 781
    directory(s).get_card()
    assert sim.logins == 2


def test_relay_side_expiry_relogs_in_once_and_resends_identical_bytes(env):
    m, sim = env
    seed_card(m, sim)
    s, _ = new(m, sim)
    s.hold()
    sim.expire_tokens()
    s.hold()
    holds = [x for x in m.seen if x.path.endswith("/host/hold")]
    assert len(holds) == 3 and holds[1].body == holds[2].body
    assert sim.logins == 2


def test_relogin_is_single_flight(env):
    m, sim = env
    seed_card(m, sim)
    s, clock = new(m, sim)
    directory(s).get_card()
    clock.t += 1000
    errors = []

    def worker():
        try:
            directory(s).get_card()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert errors == [] and sim.logins == 2


# --- holder ---------------------------------------------------------------------------------------------------------


def test_second_host_takes_over_and_the_first_is_fenced(env):
    m, sim = env
    seed_card(m, sim)
    alarms_a = Alarms()
    a, clock = new(m, sim, holder=Holder(), alarms=alarms_a)
    assert a.hold().gen == 1
    b, _ = new(m, sim, holder=Holder(gen=1), clock=clock)
    assert b.operator_takeover().gen == 2
    with pytest.raises(Fenced):
        directory(a).put_card(make_card(m.origin(), card_seq=2), first=False)
    assert alarms_a.codes() == ["fenced"] and a.fenced
    n = len(m.seen)
    with pytest.raises(Fenced):
        a.hold()  # a renewal is refused locally once fenced
    assert len(m.seen) == n
    assert directory(b).put_card(make_card(m.origin(), card_seq=2), first=False) == 2


def test_holder_conflict_waits_and_then_holds(env):
    m, sim = env
    seed_card(m, sim)
    a, clock = new(m, sim)
    a.hold()
    b, _ = new(m, sim, clock=clock)
    assert b.hold().gen == 2  # waited out A's 40 s lease (fake clock), then a new generation
    assert 40.0 in clock.slept


def test_restart_after_the_lease_ran_out_takes_over_at_g_plus_1(env):
    """orch-52's restart rule (13 Oct 2026): renew fenced, (a) and (b) hold, probe {gen:null} returns stored+1."""
    m, sim = env
    seed_card(m, sim)
    h = Holder()
    a, clock = new(m, sim, holder=h)
    a.hold()
    clock.advance(41)
    alarms = Alarms()
    restarted, _ = new(m, sim, holder=h, alarms=alarms, clock=clock)
    assert restarted.hold().gen == 2
    assert alarms == [] and h.rec.pending is None and h.rec.gen == 2


# --- first link -----------------------------------------------------------------------------------------------------


class Steps:
    def __init__(self, card, *, fail_at=None, publish=None):
        self.state, self._card, self.fail_at, self.calls = "linking", card, fail_at, []
        self._publish = publish

    def _step(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            self.fail_at = None
            raise RuntimeError(f"crash at {name}")

    def link_state(self):
        return self.state

    def card(self):
        self._step("card")
        return self._card

    def genesis(self):
        self._step("genesis")
        return GENESIS_PIN, GENESIS_CERT, GENESIS

    def publish(self, session, directory):
        self._step("publish")
        if self._publish:
            self._publish(session, directory)

    def mark_linked(self):
        self._step("mark_linked")
        self.state = "linked"


def test_first_link_happy_path(env):
    m, sim = env
    s, clock = new(m, sim)
    steps = Steps(make_card(m.origin()))
    first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert steps.state == "linked" and WS in sim.cards and WS in sim.genesis
    routes = [(x.method, x.path.rsplit("/", 1)[-1]) for x in m.seen]
    assert routes[0] == ("PUT", "card") and "authorization" not in m.seen[0].headers
    assert routes[1:] == [("POST", "challenge"), ("POST", "login"), ("POST", "hold"), ("PUT", "genesis")]
    first_link(steps, s, directory(s), make_retrier(clock), store=m.store)  # linked: nothing more
    assert len(m.seen) == 5


def test_first_link_refuses_without_the_reservation(env):
    m, sim = env
    s, clock = new(m, sim)
    steps = Steps(make_card(m.origin()))
    for state in ("", "unlinked", None):
        steps.state = state
        with pytest.raises(LinkRefused):
            first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert m.seen == []


@pytest.mark.parametrize("crash", ["card", "genesis", "publish", "mark_linked"])
def test_first_link_interrupted_after_each_step_resumes(env, crash):
    m, sim = env
    card = make_card(m.origin())
    h = Holder()
    s, clock = new(m, sim, holder=h)
    steps = Steps(card, fail_at=crash)
    with pytest.raises(RuntimeError):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert steps.state == "linking"
    s2, _ = new(m, sim, holder=h, clock=clock)  # a restart within the lease: new process state, same relay
    first_link(steps, s2, directory(s2), make_retrier(clock), store=m.store)
    assert steps.state == "linked"
    assert sim.cards[WS] == card and canon.loads_strict(sim.genesis[WS])["pin"] == GENESIS_PIN


@pytest.mark.parametrize("fault_route", ["card", "login", "hold", "genesis"])
def test_first_link_survives_a_transient_failure_on_each_call(env, fault_route):
    m, sim = env
    s, clock = new(m, sim)
    real = sim._route
    hit = []

    def flaky(seen):
        if seen.path.endswith("/" + fault_route) and not hit:
            hit.append(1)
            from .mock import json_reply

            return json_reply(503, {"error": "overloaded"}, {"Retry-After": "2"})
        return real(seen)

    sim._route = flaky
    steps = Steps(make_card(m.origin()))
    first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert steps.state == "linked" and hit == [1] and any(x >= 2 for x in clock.slept)


def test_fenced_mid_publication_stops_the_link(env):
    m, sim = env
    other = {}

    def publish(session, d):
        o, _ = new(m, sim, holder=Holder(gen=session.gen), clock=other["clock"])
        o.operator_takeover()  # a second host instance takes over mid-publication
        d.put_card(make_card(m.origin(), card_seq=2), first=False)  # the next W(g) write

    alarms = Alarms()
    s, clock = new(m, sim, alarms=alarms)
    other["clock"] = clock
    steps = Steps(make_card(m.origin()), publish=publish)
    with pytest.raises(Fenced):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert steps.state == "linking" and alarms.codes() == ["fenced"]


# --- redaction over whole flows -------------------------------------------------------------------------------------


def test_no_token_challenge_or_signature_in_logs_or_errors(env, caplog):
    caplog.set_level(logging.DEBUG, logger="orch")
    m, sim = env
    s, clock = new(m, sim)
    errors = []
    first_link(Steps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=m.store)
    sim.expire_tokens()
    s.hold()
    sim.origin = "https://elsewhere.example"
    s._token = None
    try:
        s.login()
    except Exception as e:  # noqa: BLE001
        errors.append(f"{e} {e!r}")
    secrets = [t for t in sim.tokens] + [crypto.b64u(c) for c in sim.issued]
    secrets += [canon.loads_strict(b)["sig"] for b in sim.login_bodies]
    text = caplog.text + " ".join(errors) + repr(s)
    assert secrets and not [x for x in secrets if x in text]
    assert "Bearer" not in text


class ResigningSteps(Steps):
    """A LinkSteps double that re-signs the genesis pin on every call (ECDSA signatures are randomised)."""

    def genesis(self):
        self._step("genesis")
        o = GENESIS_PIN["o"]
        sig = crypto.sign(corpus.priv("phone.sig"), b"orch/v2/sig/genesis-pin|" + canon.dumps(o))
        return {"o": o, "sig": crypto.b64u(sig)}, GENESIS_CERT, GENESIS


@pytest.mark.parametrize("crash", ["publish", "mark_linked"])
def test_resume_resends_the_persisted_genesis_pin_bytes(env, crash):
    """Review L1: the relay compares the whole pin, signature included; a re-signed pin would be a permanent
    genesis_mismatch. The first link persists the pin before its PUT and re-sends those bytes on resume."""
    m, sim = env
    h = Holder()
    s, clock = new(m, sim, holder=h)
    steps = ResigningSteps(make_card(m.origin()), fail_at=crash)
    with pytest.raises(RuntimeError):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    sent = sim.genesis[WS]
    s2, _ = new(m, sim, holder=h, clock=clock)
    first_link(steps, s2, directory(s2), make_retrier(clock), store=m.store)
    assert steps.state == "linked" and sim.genesis[WS] == sent
    assert steps.calls.count("genesis") == 1  # never asked to sign again


def test_without_persistence_a_resigned_pin_is_a_genesis_mismatch(env):
    """The hazard the persistence removes, shown against the simulated relay."""
    m, sim = env
    s, clock = new(m, sim)
    first_link(ResigningSteps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=m.store)
    pin, cert, genesis = ResigningSteps(None).genesis()
    with pytest.raises(retry.RelayRefused) as e:
        directory(s).put_genesis(pin, cert, genesis=genesis)
    assert e.value.code == "genesis_mismatch"


def test_resume_uses_the_persisted_card(env):
    m, sim = env
    h = Holder()
    s, clock = new(m, sim, holder=h)
    card = make_card(m.origin())
    steps = Steps(card, fail_at="publish")
    with pytest.raises(RuntimeError):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    steps._card = make_card(m.origin())  # a rebuilt card (another signature) must not be sent
    s2, _ = new(m, sim, holder=h, clock=clock)
    first_link(steps, s2, directory(s2), make_retrier(clock), store=m.store)
    assert sim.cards[WS] == card and steps.calls.count("card") == 1


@pytest.mark.parametrize("bad", [b"null", b"[]", b"0", b'"card"'])
def test_a_persisted_link_card_that_is_not_an_object_is_refused_before_any_request(env, bad):
    """Round 9: ``link-card.json`` holding ``null`` (or another non-object) is a :class:`LinkRefused`, never a
    TypeError later and never rebuilt; nothing is sent."""
    m, sim = env
    s, clock = new(m, sim)
    m.store.write("link-card.json", bad)
    with pytest.raises(LinkRefused, match="not a JSON object"):
        first_link(Steps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=m.store)
    assert m.seen == []
    assert m.store.read("link-card.json") == bad


def test_a_persisted_link_genesis_that_is_null_is_refused_before_put_genesis(env):
    m, sim = env
    s, clock = new(m, sim)
    m.store.write("link-genesis.json", b"null")
    with pytest.raises(LinkRefused, match="not a JSON object"):
        first_link(Steps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=m.store)
    assert not any(x.path.endswith("/genesis") for x in m.seen)
