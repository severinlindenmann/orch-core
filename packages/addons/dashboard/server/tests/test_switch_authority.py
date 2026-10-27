"""Run 12: "switch is navigation; authority stays with the workspace". A browser key enrolled by a REAL local sign-in is
what upgrades a hub-made (switch) session; nothing else does."""

import base64
import json

import pytest
from conftest import ORIGIN, PORT
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient
from test_dashboard_switch import TICKET, WS, Gw, Link

from orch_dashboard import trusted
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth

H = {"Origin": ORIGIN}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Browser:
    """What the page does with WebCrypto: a P-256 key pair, ECDSA over the challenge message."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64(n.x.to_bytes(32, "big")),
            "y": b64(n.y.to_bytes(32, "big")),
        }

    def sign(self, nonce, origin=ORIGIN):
        r, s = utils.decode_dss_signature(self.key.sign(trusted.message(nonce, origin), ec.ECDSA(hashes.SHA256())))
        return b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


@pytest.fixture
def now():
    return [1_000_000.0]


@pytest.fixture
def auth(now, tmp_path):
    return Auth(clock=lambda: now[0], trust_file=tmp_path / "trusted.json")


@pytest.fixture
def link():
    return Link()


@pytest.fixture
def client(link, auth, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    with TestClient(create_app(PORT, Gw(link, state), auth, static), base_url=ORIGIN) as c:
        yield c


def bearer(token):
    return {**H, "Authorization": "Bearer " + token}


def local_session(auth):
    return auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]


def switch_session(client, link):
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": "/", "kind": "open"}
    return client.post("/auth/switch", json={"t": TICKET}, headers=H).json()["token"]


def enrol(client, auth, browser):
    token = local_session(auth)
    res = client.post("/api/trusted/enrol", json={"jwk": browser.jwk}, headers=bearer(token))
    assert res.status_code == 200, res.text
    return token, res.json()["key_id"]


def upgrade(client, token, browser, kid, origin=ORIGIN):
    nonce = client.post("/api/auth/challenge", json={}, headers=bearer(token)).json()["nonce"]
    return client.post(
        "/api/auth/upgrade", json={"key_id": kid, "signature": browser.sign(nonce, origin)}, headers=bearer(token)
    )


WRITE = f"/api/w/{WS}/worktrees/remove"


def refused_as_step_up(res):
    assert res.status_code == 403
    err = res.json()["error"]
    assert err["code"] == "step_up_required" and err["step_up"]["where"] == "this_computer"
    return True


def test_same_browser_switch_is_a_full_session_with_zero_prompts(client, link, auth):
    b = Browser()
    _, kid = enrol(client, auth, b)
    sw = switch_session(client, link)
    assert refused_as_step_up(client.post(WRITE, json={}, headers=bearer(sw)))
    res = upgrade(client, sw, b, kid)
    assert res.status_code == 200
    full = res.json()["token"]
    assert auth.login_kind(full) == "local" and auth.switch_workspace(full) is None
    assert not auth.valid(sw)  # the switch session ended
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code != 403  # reaches the route: not a step-up


def test_a_fresh_profile_is_read_mostly_and_gets_a_structured_step_up(client, link, auth):
    sw = switch_session(client, link)
    assert client.get(f"/api/w/{WS}/head", headers=bearer(sw)).status_code != 403
    assert client.get("/api/host", headers=bearer(sw)).status_code != 403  # Settings › General reads it
    for method, path in (
        ("POST", WRITE),
        ("POST", f"/api/w/{WS}/terminals/open"),
        ("POST", f"/api/w/{WS}/agents/start"),
    ):
        assert refused_as_step_up(client.request(method, path, json={}, headers=bearer(sw)))
    assert client.post("/api/auth/challenge", json={}, headers=bearer(sw)).status_code == 200
    # no key: the upgrade fails whatever it signs
    res = client.post("/api/auth/upgrade", json={"key_id": "0" * 32, "signature": "A" * 86}, headers=bearer(sw))
    assert res.status_code == 401


def test_a_switch_session_cannot_enrol_a_key_and_then_upgrade_with_it(client, link, auth):
    b = Browser()
    sw = switch_session(client, link)
    assert refused_as_step_up(client.post("/api/trusted/enrol", json={"jwk": b.jwk}, headers=bearer(sw)))
    assert auth.enrol_key(sw, b.jwk, ORIGIN) is None  # nor directly
    assert auth.trusted_list() == []
    # a session a key upgraded cannot enrol more keys either
    b2 = Browser()
    _, kid = enrol(client, auth, b2)
    full = upgrade(client, switch_session(client, link), b2, kid).json()["token"]
    assert auth.enrol_key(full, b.jwk, ORIGIN) is None


def test_a_replayed_hub_session_cannot_write_sign_or_open_a_terminal_even_on_loopback(client, link, auth):
    b, thief = Browser(), Browser()
    _, kid = enrol(client, auth, b)
    sw = switch_session(client, link)  # the replayed hub session: no key of its own
    # the thief signs with a key the server never enrolled, or with the right id but the wrong key
    assert upgrade(client, sw, thief, kid).status_code == 401
    assert upgrade(client, sw, thief, trusted.key_id(thief.jwk)).status_code == 401
    for path in (WRITE, f"/api/w/{WS}/terminals/open", f"/api/w/{WS}/click/sign", f"/api/w/{WS}/runs/x/attach"):
        assert refused_as_step_up(client.post(path, json={}, headers=bearer(sw)))
    assert auth.login_kind(sw) == "switch"


def test_a_challenge_is_single_use_and_bound_to_the_origin(client, link, auth):
    b = Browser()
    _, kid = enrol(client, auth, b)
    sw = switch_session(client, link)
    nonce = client.post("/api/auth/challenge", json={}, headers=bearer(sw)).json()["nonce"]
    good = {"key_id": kid, "signature": b.sign(nonce)}
    assert client.post("/api/auth/upgrade", json=good, headers=bearer(sw)).status_code == 200
    assert client.post("/api/auth/upgrade", json=good, headers=bearer(sw)).status_code == 401  # gone with the session
    sw2 = switch_session(client, link)
    n2 = client.post("/api/auth/challenge", json={}, headers=bearer(sw2)).json()["nonce"]
    wrong = {"key_id": kid, "signature": b.sign(n2, "http://127.0.0.1:1")}
    assert client.post("/api/auth/upgrade", json=wrong, headers=bearer(sw2)).status_code == 401
    # and the failed try spent the challenge
    assert (
        client.post("/api/auth/upgrade", json={"key_id": kid, "signature": b.sign(n2)}, headers=bearer(sw2)).status_code
        == 401
    )


def test_a_stale_challenge_is_refused(client, link, auth, now):
    b = Browser()
    _, kid = enrol(client, auth, b)
    sw = switch_session(client, link)
    nonce = client.post("/api/auth/challenge", json={}, headers=bearer(sw)).json()["nonce"]
    now[0] += 61
    res = client.post("/api/auth/upgrade", json={"key_id": kid, "signature": b.sign(nonce)}, headers=bearer(sw))
    assert res.status_code == 401


def test_revoking_a_browser_ends_the_sessions_it_upgraded(client, link, auth):
    b = Browser()
    local, kid = enrol(client, auth, b)
    full = upgrade(client, switch_session(client, link), b, kid).json()["token"]
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code != 403
    assert client.get("/api/trusted", headers=bearer(local)).json()[0]["id"] == kid
    assert client.delete(f"/api/trusted/{kid}", headers=bearer(local)).json() == {"revoked": True}
    assert not auth.valid(full)  # ended: its terminal sockets and event streams watch `valid` and close with it
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code == 401
    # and a new switch cannot come back up with the revoked key
    assert upgrade(client, switch_session(client, link), b, kid).status_code == 401


def test_a_trusted_browser_idle_for_30_days_is_forgotten(client, link, auth, now):
    b = Browser()
    local, kid = enrol(client, auth, b)
    now[0] += trusted.IDLE - 1
    assert [r["id"] for r in auth.trusted_list()] == [kid]
    now[0] += 2
    assert auth.trusted_list() == []
    assert upgrade(client, switch_session(client, link), b, kid).status_code == 401


def test_using_a_key_keeps_it_alive_and_it_survives_a_restart(client, link, auth, now, tmp_path):
    b = Browser()
    _, kid = enrol(client, auth, b)
    now[0] += trusted.IDLE - 10
    assert upgrade(client, switch_session(client, link), b, kid).status_code == 200
    now[0] += trusted.IDLE - 10  # would be past the first window, but the use renewed it
    again = Auth(clock=lambda: now[0], trust_file=tmp_path / "trusted.json")
    assert [r["id"] for r in again.trusted_list()] == [kid]
    assert oct((tmp_path / "trusted.json").stat().st_mode & 0o777) == "0o600"
    assert "d" not in json.loads((tmp_path / "trusted.json").read_text())["keys"][kid]["jwk"]  # public half only


def test_junk_keys_are_refused(client, auth):
    local = local_session(auth)
    for jwk in ({}, {"kty": "RSA"}, {"kty": "EC", "crv": "P-256", "x": "A" * 43, "y": "A" * 43}):
        assert client.post("/api/trusted/enrol", json={"jwk": jwk}, headers=bearer(local)).status_code == 403


def test_no_hub_means_a_local_session_is_what_it_was(client, auth):
    local = local_session(auth)
    assert auth.login_kind(local) == "local"
    assert client.post(WRITE, json={}, headers=bearer(local)).status_code != 403


def test_a_key_idle_expiry_ends_the_session_it_upgraded(client, link, auth, now):
    b = Browser()
    _, kid = enrol(client, auth, b)
    full = upgrade(client, switch_session(client, link), b, kid).json()["token"]
    now[0] += trusted.IDLE + 1
    assert not auth.valid(full)


def test_an_upgraded_session_is_pinned_to_loopback_and_holds_no_step_up(client, link, auth):
    b = Browser()
    _, kid = enrol(client, auth, b)
    sw = switch_session(client, link)
    # a step-up the switch session might hold must not carry over
    auth.stepups[sw] = {("a", "d"): (auth.clock(), "proof", None)}
    full = upgrade(client, sw, b, kid).json()["token"]
    assert auth.listener_of(full) == "loopback"
    assert auth.stepups.get(full) is None
    assert auth.stepup_take(full, "a", "d") == (False, None)


def test_host_info_for_a_switch_session_is_one_workspace_only(client, link, auth, monkeypatch):
    other = "c" * 32
    info = {
        "dev": {"sim_convenience": True, "sandbox": "abcd1234"},
        "alarms": [{"code": "a_one", "workspace_id": WS}, {"code": "b_two", "workspace_id": other}, {"code": "c_host"}],
    }
    monkeypatch.setattr(Gw, "host_info", lambda self: info, raising=False)
    sw = switch_session(client, link)
    got = client.get("/api/host", headers=bearer(sw)).json()
    assert set(got) == {"dev", "alarms"} and got["alarms"] == [{"code": "a_one", "workspace_id": WS}]
    full = client.get("/api/host", headers=bearer(local_session(auth))).json()
    assert len(full["alarms"]) == 3


def test_two_processes_share_the_file_without_resurrecting_a_revoked_key(now, tmp_path):
    f = tmp_path / "t.json"
    a, b = Auth(clock=lambda: now[0], trust_file=f), Auth(clock=lambda: now[0], trust_file=f)
    k1, k2 = Browser(), Browser()
    kid1 = a.enrol_key(local_session(a), k1.jwk, ORIGIN)
    assert [r["id"] for r in b.trusted_list()] == [kid1]  # B sees A's key without restarting
    kid2 = b.enrol_key(local_session(b), k2.jwk, ORIGIN)
    assert a.revoke_key(kid1) is True
    b.trusted.touch(kid2)  # B writes after A's revoke: it must not bring kid1 back
    assert [r["id"] for r in b.trusted_list()] == [kid2]
    assert [r["id"] for r in Auth(clock=lambda: now[0], trust_file=f).trusted_list()] == [kid2]


def test_a_missing_host_header_is_not_a_server_error(client, auth):
    local = local_session(auth)
    res = client.post("/api/trusted/enrol", json={"jwk": {}}, headers={**bearer(local)})
    assert res.status_code < 500


def test_trusted_imports_and_locks_without_fcntl(monkeypatch, tmp_path):
    """Windows has no fcntl: the module must still import, and the lock falls back to msvcrt."""
    import importlib
    import sys
    import types

    calls = []
    fake = types.ModuleType("msvcrt")
    fake.LK_LOCK = 1
    fake.locking = lambda fd, mode, n: calls.append((mode, n))
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` raises ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    mod = importlib.reload(trusted)
    try:
        assert mod.fcntl is None
        store = mod.TrustedKeys(tmp_path / "t.json", lambda: 1.0)
        b = Browser()
        kid = store.enrol(mod.clean_jwk(b.jwk), ORIGIN)
        assert store.remove(kid) and calls  # a change took the msvcrt lock
    finally:
        monkeypatch.undo()
        importlib.reload(trusted)
