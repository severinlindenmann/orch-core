"""Run 12 + E4/D4: "switch is navigation; authority stays with the workspace".
A hub switch link opens a FULL session with
no question only when the browser proves a key that a REAL local sign-in enrolled for this origin, BEFORE the ticket is
redeemed. Any failure leaves the ticket unused (the page asks "Open <name>?", a read-mostly session). There is no other
way up: `/api/auth/upgrade` and `/api/auth/challenge` are gone."""

import base64
import json

import pytest
from conftest import ORIGIN, PORT
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_dashboard_switch import TICKET, WS, Gw, Link

from orch_dashboard import routegate, trusted
from orch_dashboard.app import create_app
from orch_dashboard.auth import SWITCH_CHALLENGE_GLOBAL, SWITCH_CHALLENGE_RATE, Auth
from orch_dashboard.errors import fail
from orch_dashboard.gateway import WRITE_OPS, Gateway
from orch_dashboard.security import SWITCH_GLOBAL

H = {"Origin": ORIGIN}
PERSON = "p_owner1"
OTHER_TICKET = WS + "." + "e" * 32


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Browser:
    """What the page does with WebCrypto: a P-256 key pair, ECDSA over the redeem message."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        n = self.key.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64(n.x.to_bytes(32, "big")),
            "y": b64(n.y.to_bytes(32, "big")),
        }
        self.kid = trusted.key_id(self.jwk)

    def sign(self, nonce, t=TICKET, origin=ORIGIN, ws=WS, kid=None):
        msg = trusted.message(nonce, origin, ws, trusted.ticket_hash(t), kid or self.kid)
        r, s = utils.decode_dss_signature(self.key.sign(msg, ec.ECDSA(hashes.SHA256())))
        return b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class KeyLink(Link):
    """The workspace's host link: the hub switch op (answers set per test), plus who this computer's user is there,
    the roster and the workspace name. Every other op and every write is recorded."""

    def __init__(self):
        super().__init__()
        self.welcome = {"peer": {"person": PERSON}}
        self.members = [{"person": PERSON, "name": "Ann", "role": "owner", "devices": []}]
        self.other_ops, self.writes = [], []

    def call(self, op, args=None, **kw):
        if kw.get("write") or op in WRITE_OPS:
            self.writes.append(op)
        if op == "member.list":
            return {"members": self.members}
        if op == "workspace.show":
            return {"name": "Product", "prefix": "PRD"}
        if op != "hub.switch":
            self.other_ops.append(op)
            raise fail("not_found")
        return super().call(op, args, **kw)


class PeopleGw(Gw):
    people_now = {WS: PERSON}
    served = (WS,)  # the workspaces this computer hosts (all through the one fake link)

    def people(self):
        return dict(self.people_now)

    def enabled(self, ws):
        if ws not in self.served:
            raise fail("not_found")
        return self._l


@pytest.fixture
def now():
    return [1_000_000.0]


@pytest.fixture
def auth(now, tmp_path):
    return Auth(clock=lambda: now[0], trust_file=tmp_path / "trusted.json")


@pytest.fixture
def link():
    link = KeyLink()
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": "/", "kind": "open"}
    return link


@pytest.fixture
def client(link, auth, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    with TestClient(create_app(PORT, PeopleGw(link, state), auth, static), base_url=ORIGIN) as c:
        yield c


def bearer(token):
    return {**H, "Authorization": "Bearer " + token}


def local_session(auth):
    return auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]


def redeems(link):
    return [c for c in link.calls if c["kind"] == "redeem"]


def switch_session(client, t=TICKET):
    """The "Open <name>?" button: the ticket alone, no proof: a read-mostly session."""
    res = client.post("/auth/switch", json={"t": t}, headers=H)
    assert res.status_code == 200, res.text
    return res.json()["token"]


def enrol(client, auth, browser):
    token = local_session(auth)
    res = client.post("/api/trusted/enrol", json={"jwk": browser.jwk}, headers=bearer(token))
    assert res.status_code == 200, res.text
    return token, res.json()["key_id"]


def challenge(client, t=TICKET):
    res = client.post(
        "/auth/switch/challenge", json={"ticket_hash": trusted.ticket_hash(t), "workspace_id": WS}, headers=H
    )
    assert res.status_code == 200, res.text
    return res.json()


def silent(client, browser, t=TICKET, nonce=None, kid=None, **sign):
    """What the page does on a rail click: a challenge for the ticket's hash, the signature, then the ticket."""
    nonce = nonce or challenge(client, t)["nonce"]
    body = {"t": t, "key_id": kid or browser.kid, "sig": browser.sign(nonce, t=t, kid=kid, **sign)}
    return client.post("/auth/switch", json=body, headers=H)


def unverified(res):
    assert res.status_code == 401 and res.json()["error"]["code"] == "switch.unverified", res.text
    return True


WRITE = f"/api/w/{WS}/worktrees/remove"


def refused_as_step_up(res):
    assert res.status_code == 403
    err = res.json()["error"]
    assert err["code"] == "step_up_required" and err["step_up"]["where"] == "this_computer"
    return True


# --- the silent path ----------------------------------------------------------------------------------------------


def test_same_browser_lands_full_with_no_question_and_one_redeem(client, link, auth):
    b = Browser()
    _, kid = enrol(client, auth, b)
    got = challenge(client)
    assert got["name"] == "Product" and redeems(link) == []  # asking for a nonce uses nothing
    res = silent(client, b, nonce=got["nonce"])
    assert res.status_code == 200, res.text
    full = res.json()["token"]
    assert res.json()["workspace_id"] == WS and res.json()["return_to"] == "/"
    assert auth.login_kind(full) == "local" and auth.switch_workspace(full) is None
    assert auth.keyed[full] == (kid, WS)
    assert redeems(link) == [{"kind": "redeem", "ticket": TICKET}]
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code != 403  # reaches the route: not a step-up


def test_the_silent_session_is_pinned_to_loopback_and_holds_no_step_up(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    full = silent(client, b).json()["token"]
    assert auth.listener_of(full) == "loopback"
    assert auth.stepups.get(full) is None
    assert auth.stepup_take(full, "a", "d") == (False, None)


def test_a_signout_ticket_with_a_proof_opens_nothing(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": "/", "kind": "signout"}
    before = set(auth.sessions)
    assert silent(client, b).json() == {"signed_out": True}
    assert set(auth.sessions) == before


# --- every failed proof: the confirm is shown and the ticket is still good ----------------------------------------


def still_good(client, link, t=TICKET):
    """No redeem reached the hub, and the "Open" button can still use the ticket (read-mostly)."""
    assert redeems(link) == []
    sw = switch_session(client, t)
    assert redeems(link) == [{"kind": "redeem", "ticket": t}]
    return sw


def test_no_key_at_all_is_the_confirm_and_a_read_mostly_session(client, link, auth):
    stranger = Browser()  # never enrolled
    assert unverified(silent(client, stranger))
    sw = still_good(client, link)
    assert auth.login_kind(sw) == "switch"
    assert client.get(f"/api/w/{WS}/head", headers=bearer(sw)).status_code != 403
    assert client.get("/api/host", headers=bearer(sw)).status_code != 403  # Settings › General reads it
    for method, path in (
        ("POST", WRITE),
        ("POST", f"/api/w/{WS}/terminals/open"),
        ("POST", f"/api/w/{WS}/agents/start"),
        ("POST", f"/api/w/{WS}/click/sign"),
        ("POST", f"/api/w/{WS}/runs/x/attach"),
    ):
        assert refused_as_step_up(client.request(method, path, json={}, headers=bearer(sw)))


def test_wrong_origin_is_the_confirm(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    assert unverified(silent(client, b, origin="http://127.0.0.1:1"))
    still_good(client, link)


def test_a_key_enrolled_for_another_origin_is_the_confirm(client, link, auth):
    b = Browser()
    auth.enrol_key(local_session(auth), b.jwk, "http://127.0.0.1:1", {WS: PERSON})
    assert unverified(silent(client, b))
    still_good(client, link)


def test_wrong_ticket_hash_is_the_confirm(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    nonce = challenge(client, OTHER_TICKET)["nonce"]  # a nonce for another ticket
    assert unverified(silent(client, b, nonce=nonce))
    # and a signature over another ticket's hash does not fit this one
    nonce = challenge(client)["nonce"]
    body = {"t": TICKET, "key_id": b.kid, "sig": b.sign(nonce, t=OTHER_TICKET)}
    assert unverified(client.post("/auth/switch", json=body, headers=H))
    still_good(client, link)


def test_a_replayed_nonce_is_the_confirm(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    nonce = challenge(client)["nonce"]
    assert unverified(silent(client, b, nonce=nonce, origin="http://127.0.0.1:1"))  # spent by the failed try
    assert unverified(silent(client, b, nonce=nonce))  # the right signature over a spent nonce
    still_good(client, link)
    # a nonce that WORKED cannot open a second session either
    link.calls.clear()
    nonce = challenge(client, OTHER_TICKET)["nonce"]
    assert silent(client, b, t=OTHER_TICKET, nonce=nonce).status_code == 200
    assert unverified(silent(client, b, t=OTHER_TICKET, nonce=nonce))
    assert len(redeems(link)) == 1


def test_a_stale_nonce_is_the_confirm(client, link, auth, now):
    b = Browser()
    enrol(client, auth, b)
    nonce = challenge(client)["nonce"]
    now[0] += 61
    assert unverified(silent(client, b, nonce=nonce))
    still_good(client, link)


def test_a_revoked_key_is_the_confirm(client, link, auth):
    b = Browser()
    local, kid = enrol(client, auth, b)
    assert client.delete(f"/api/trusted/{kid}", headers=bearer(local)).json() == {"revoked": True}
    assert unverified(silent(client, b))
    still_good(client, link)


def test_a_thief_key_or_a_borrowed_key_id_is_the_confirm(client, link, auth):
    b, thief = Browser(), Browser()
    enrol(client, auth, b)
    assert unverified(silent(client, thief, kid=b.kid))  # the right id, the wrong key
    assert unverified(silent(client, thief))  # a key the server never enrolled
    body = {"t": TICKET, "key_id": b.kid, "sig": "A" * 86}
    challenge(client)
    assert unverified(client.post("/auth/switch", json=body, headers=H))
    assert unverified(client.post("/auth/switch", json={"t": TICKET, "key_id": b.kid}, headers=H))  # half a proof
    still_good(client, link)


def test_the_key_person_must_be_this_dashboards_person(client, link, auth):
    b = Browser()
    PeopleGw.people_now = {WS: "p_someoneelse"}
    try:
        enrol(client, auth, b)  # enrolled while this computer's user was another person in the workspace
    finally:
        PeopleGw.people_now = {WS: PERSON}
    assert unverified(silent(client, b))
    still_good(client, link)


def test_a_key_enrolled_before_the_workspace_existed_is_the_confirm(client, link, auth):
    b = Browser()
    PeopleGw.people_now = {}
    try:
        enrol(client, auth, b)
    finally:
        PeopleGw.people_now = {WS: PERSON}
    assert unverified(silent(client, b))
    still_good(client, link)


def test_a_key_enrolled_without_this_workspace_is_refused(client, link, auth):
    b = Browser()
    PeopleGw.people_now = {"c" * 32: PERSON}  # the same person, but only in another workspace
    try:
        _, kid = enrol(client, auth, b)
    finally:
        PeopleGw.people_now = {WS: PERSON}
    assert WS not in auth.trusted.get(kid)["people"]
    assert unverified(silent(client, b))
    still_good(client, link)


def test_a_proof_for_workspace_a_replayed_with_a_ticket_for_b_is_the_confirm(client, link, auth, monkeypatch):
    other_ws = "c" * 32
    ticket_b = other_ws + "." + "d" * 32
    monkeypatch.setattr(PeopleGw, "served", (WS, other_ws))
    monkeypatch.setattr(PeopleGw, "people_now", {WS: PERSON, other_ws: PERSON})
    b = Browser()
    enrol(client, auth, b)
    # a whole proof made for A's ticket, presented with B's ticket
    nonce = challenge(client)["nonce"]
    proof_a = {"key_id": b.kid, "sig": b.sign(nonce)}
    assert unverified(client.post("/auth/switch", json={"t": ticket_b, **proof_a}, headers=H))
    # B's own challenge, signed as if for workspace A
    nonce = challenge(client, ticket_b)["nonce"]
    body = {"t": ticket_b, "key_id": b.kid, "sig": b.sign(nonce, t=ticket_b, ws=WS)}
    assert unverified(client.post("/auth/switch", json=body, headers=H))
    assert not auth.keyed
    sw = still_good(client, link, ticket_b)  # B's ticket is unused: the confirm can still open it
    assert auth.login_kind(sw) == "switch" and auth.switch_workspace(sw) == other_ws


def test_the_hub_is_not_contacted_when_the_switch_is_unverified(client, link, auth):
    b, stranger = Browser(), Browser()
    enrol(client, auth, b)
    assert unverified(silent(client, stranger))  # unknown key
    assert unverified(silent(client, b, origin="http://127.0.0.1:1"))  # bad signature
    assert unverified(client.post("/auth/switch", json={"t": TICKET, "key_id": b.kid}, headers=H))  # half a proof
    assert link.calls == []  # no hub.switch of any kind reached the host, so nothing went on to the hub


def test_the_local_person_must_be_the_workspace_owner_the_ticket_is_for(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    link.members = [{"person": PERSON, "name": "Ann", "role": "maintainer", "devices": []}]
    assert unverified(silent(client, b))
    link.members = [
        {"person": PERSON, "name": "Ann", "role": "superuser", "devices": []}
    ]  # a roster that does not parse
    assert unverified(silent(client, b))
    link.members = [{"person": "p_boss", "name": "Bo", "role": "owner", "devices": []}]
    assert unverified(silent(client, b))
    link.welcome = {"peer": {"person": "p_boss"}}  # the link speaks for the owner, but the key was enrolled for PERSON
    assert unverified(silent(client, b))
    still_good(client, link)


def test_remote_listeners_never_redeem_silently(auth):
    """The session kind follows the listener: remote listeners have no keys, so no challenge and no proof there."""
    b = Browser()
    auth.enrol_key(local_session(auth), b.jwk, ORIGIN, {WS: PERSON})
    assert auth.switch_challenge(trusted.ticket_hash(TICKET), ORIGIN, "remote") is None
    nonce = auth.switch_challenge(trusted.ticket_hash(TICKET), ORIGIN, "loopback")
    assert not auth.switch_proof(TICKET, WS, b.kid, b.sign(nonce), ORIGIN, "remote", PERSON)
    assert routegate.rule_of("POST", "/auth/switch/challenge") == routegate.REFUSED


def test_challenges_are_rate_limited_and_bounded(client, auth, now):
    for i in range(SWITCH_CHALLENGE_GLOBAL):
        challenge(client, WS + "." + f"{i:032x}")
    res = client.post("/auth/switch/challenge", json={"ticket_hash": "f" * 64, "workspace_id": WS}, headers=H)
    assert res.status_code == 429
    assert len(auth.switch_challenge_keys) <= SWITCH_CHALLENGE_GLOBAL
    now[0] += 61
    assert challenge(client)["nonce"]
    assert len(auth.switch_challenges) <= 64
    assert len(auth.switch_challenge_keys) == 1  # the idle buckets were forgotten


def test_spamming_one_tickets_challenges_does_not_block_another(client, link, auth, now):
    spam = trusted.ticket_hash(OTHER_TICKET)
    for _ in range(SWITCH_CHALLENGE_RATE):
        challenge(client, OTHER_TICKET)
    for _ in range(20):
        res = client.post("/auth/switch/challenge", json={"ticket_hash": spam, "workspace_id": WS}, headers=H)
        assert res.status_code == 429
    assert auth.switch_challenge(spam, "http://127.0.0.1:1", "loopback")  # the same hash from another origin: its own
    b = Browser()
    enrol(client, auth, b)
    assert silent(client, b).status_code == 200  # another ticket still gets its challenge and lands
    now[0] += 61
    assert challenge(client, OTHER_TICKET)["nonce"]  # the spammed ticket's own bucket empties after a minute


def test_an_invalid_ticket_with_a_good_proof_is_an_error_not_a_session(client, link, auth):
    b = Browser()
    enrol(client, auth, b)
    link.answers["redeem"] = fail("hub.refused", "the hub is refused: bad_ticket")
    res = silent(client, b)
    assert res.status_code == 401 and res.json()["error"]["code"] == "session.required"
    assert not auth.keyed


# --- enrolment, revocation and idle time ---------------------------------------------------------------------------


def test_a_switch_session_cannot_enrol_a_key_nor_can_a_key_opened_one(client, link, auth):
    b = Browser()
    sw = switch_session(client)
    assert refused_as_step_up(client.post("/api/trusted/enrol", json={"jwk": b.jwk}, headers=bearer(sw)))
    assert auth.enrol_key(sw, b.jwk, ORIGIN) is None  # nor directly
    assert auth.trusted_list() == []
    b2 = Browser()
    enrol(client, auth, b2)
    full = silent(client, b2).json()["token"]
    assert auth.enrol_key(full, b.jwk, ORIGIN) is None


def test_a_malformed_pin_row_is_skipped_and_enrolment_still_answers(auth, tmp_path):
    import os

    uid = os.getuid()
    good = {"workspace_id": WS, "host_uid": uid, "peers": {str(uid): {"class": "person", "person": PERSON}}}
    rows = [
        {"workspace_id": "1" * 32, "host_uid": uid},  # no peers
        {"workspace_id": "2" * 32, "host_uid": uid, "peers": {str(uid): {"class": "person"}}},  # no person
        {"workspace_id": "3" * 32, "host_uid": uid, "peers": ["not", "a", "map"]},
        {"workspace_id": "4" * 32, "host_uid": uid, "peers": {str(uid): "p_x"}},
        {"workspace_id": "5" * 32, "host_uid": uid, "peers": {str(uid): {"class": "person", "person": 7}}},
        {"workspace_id": 5, "host_uid": uid, "peers": {}},
        {"workspace_id": "6" * 32, "host_uid": str(uid), "peers": good["peers"]},
        {"workspace_id": "../x", "host_uid": uid, "peers": good["peers"]},
        "a row that is not an object",
        None,
        good,
    ]
    state = tmp_path / "state"
    (state / "host").mkdir(parents=True, mode=0o700)
    (state / "host/workspaces.json").write_text(json.dumps({"workspaces": rows}))
    (state / "host/workspaces.json").chmod(0o600)
    gateway = Gateway(state)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    try:
        assert gateway.people() == {WS: PERSON}
        with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as c:
            b = Browser()
            _, kid = enrol(c, auth, b)
        assert kid == b.kid and auth.trusted.get(kid)["people"] == {WS: PERSON}
    finally:
        gateway.close()


def test_the_enrolled_person_is_kept_with_the_key(client, auth, tmp_path):
    b = Browser()
    _, kid = enrol(client, auth, b)
    assert json.loads((tmp_path / "trusted.json").read_text())["keys"][kid]["people"] == {WS: PERSON}


def test_revoking_a_browser_ends_the_sessions_it_opened(client, link, auth):
    b = Browser()
    local, kid = enrol(client, auth, b)
    full = silent(client, b).json()["token"]
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code != 403
    assert client.get("/api/trusted", headers=bearer(local)).json()[0]["id"] == kid
    assert client.delete(f"/api/trusted/{kid}", headers=bearer(local)).json() == {"revoked": True}
    assert not auth.valid(full)  # ended: its terminal sockets and event streams watch `valid` and close with it
    assert client.post(WRITE, json={}, headers=bearer(full)).status_code == 401


def test_a_trusted_browser_idle_for_30_days_is_forgotten(client, link, auth, now):
    b = Browser()
    _, kid = enrol(client, auth, b)
    now[0] += trusted.IDLE - 1
    assert [r["id"] for r in auth.trusted_list()] == [kid]
    now[0] += 2
    assert auth.trusted_list() == []
    assert unverified(silent(client, b))
    assert redeems(link) == []


def test_a_redeem_renews_the_key_and_it_survives_a_restart(client, link, auth, now, tmp_path):
    b = Browser()
    _, kid = enrol(client, auth, b)
    now[0] += trusted.IDLE - 10
    assert silent(client, b).status_code == 200
    assert auth.trusted_list()[0]["last_used"] == now[0]  # the use refreshed its idle time
    now[0] += trusted.IDLE - 10  # would be past the first window, but the use renewed it
    again = Auth(clock=lambda: now[0], trust_file=tmp_path / "trusted.json")
    assert [r["id"] for r in again.trusted_list()] == [kid]
    assert oct((tmp_path / "trusted.json").stat().st_mode & 0o777) == "0o600"
    assert "d" not in json.loads((tmp_path / "trusted.json").read_text())["keys"][kid]["jwk"]  # public half only


def test_a_failed_proof_does_not_renew_the_key(client, link, auth, now):
    b = Browser()
    enrol(client, auth, b)
    born = auth.trusted_list()[0]["last_used"]
    now[0] += 100
    assert unverified(silent(client, b, origin="http://127.0.0.1:1"))
    assert auth.trusted_list()[0]["last_used"] == born


def test_a_key_idle_expiry_ends_the_session_it_opened(client, link, auth, now):
    b = Browser()
    enrol(client, auth, b)
    full = silent(client, b).json()["token"]
    now[0] += trusted.IDLE + 1
    assert not auth.valid(full)


# --- D4: the upgrade path is gone -----------------------------------------------------------------------------------


def test_the_upgrade_path_is_gone(client, link, auth):
    paths = {r.path for r in client.app.routes if isinstance(r, APIRoute)}
    for gone in ("/api/auth/upgrade", "/api/auth/challenge"):
        assert gone not in paths
        assert all(template != gone for _, template in routegate.TABLE)
        assert all(template != gone for _, template in SWITCH_GLOBAL)
        for token in (switch_session(client), local_session(auth)):
            before = set(auth.sessions)
            res = client.post(gone, json={"key_id": "0" * 32, "signature": "A" * 86}, headers=bearer(token))
            # 405: only the SPA's GET catch-all matches that path now; 403: a switch session is refused before routing
            assert res.status_code in (403, 404, 405) and "token" not in res.text
            assert set(auth.sessions) == before
    assert b"orch-switch-upgrade" not in trusted.DOMAIN and trusted.DOMAIN == b"orch-switch-redeem-v1\0"


# --- no route acts on load, whatever its return_to ------------------------------------------------------------------

STREAMS = {"/api/events"}


def test_no_route_acts_on_load_whatever_its_return_to(client, link, auth, now):
    """A forced navigation into one's own workspace at any `return_to` path (X8's residual): landing there (the GET the
    browser makes, with the full session) writes nothing on the host and asks the hub for nothing more."""
    b = Browser()
    enrol(client, auth, b)
    walked = 0
    for route in client.app.routes:
        template = getattr(route, "path", "")
        if not isinstance(route, APIRoute) or "GET" not in route.methods or template in STREAMS:
            continue
        path = template.replace("{ws}", WS)
        for name in ("key", "run", "name", "page", "tab", "ticket", "id", "route:path", "path:path", "rest:path"):
            path = path.replace("{" + name + "}", "x")
        path = path.replace("{", "").replace("}", "")
        now[0] += 61  # past the per-minute limiters: every landing is its own rail click
        link.calls.clear()
        link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": path, "kind": "open"}
        res = silent(client, b)
        assert res.status_code == 200, (template, res.text)
        landing = res.json()["return_to"]
        assert landing in (path, "/")
        client.get(landing, headers=bearer(res.json()["token"]))
        walked += 1
        assert link.writes == [], (template, link.writes)
        kinds = [c["kind"] for c in link.calls]
        assert kinds[0] == "redeem" and set(kinds[1:]) <= {"rail"}, (
            template,
            kinds,
        )  # the rail is a read; no new ticket
    assert walked > 40


# --- unrelated to the switch, kept from run 12 ----------------------------------------------------------------------


def test_host_info_for_a_switch_session_is_one_workspace_only(client, link, auth, monkeypatch):
    other = "c" * 32
    info = {
        "dev": {"sim_convenience": True, "sandbox": "abcd1234"},
        "alarms": [{"code": "a_one", "workspace_id": WS}, {"code": "b_two", "workspace_id": other}, {"code": "c_host"}],
    }
    monkeypatch.setattr(Gw, "host_info", lambda self: info, raising=False)
    sw = switch_session(client)
    got = client.get("/api/host", headers=bearer(sw)).json()
    assert set(got) == {"dev", "alarms"} and got["alarms"] == [{"code": "a_one", "workspace_id": WS}]
    full = client.get("/api/host", headers=bearer(local_session(auth))).json()
    assert len(full["alarms"]) == 3


def test_junk_keys_are_refused(client, auth):
    local = local_session(auth)
    for jwk in ({}, {"kty": "RSA"}, {"kty": "EC", "crv": "P-256", "x": "A" * 43, "y": "A" * 43}):
        assert client.post("/api/trusted/enrol", json={"jwk": jwk}, headers=bearer(local)).status_code == 403


def test_no_hub_means_a_local_session_is_what_it_was(client, auth):
    local = local_session(auth)
    assert auth.login_kind(local) == "local"
    assert client.post(WRITE, json={}, headers=bearer(local)).status_code != 403


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
