"""#1198 R1-5, R1-6: the step-up list (`stepup.STEP_UP`) is enforced for every variant by one helper every handler of
the list calls first, and a remote session passes only with a passkey assertion made for exactly that operation and
review, used once, at most ten minutes old. Local sessions need none; plain variants (off, the reviews) stay plain."""

# ruff: noqa: F811

import ast
import base64
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute
from passkey_kit import JSON, auth, clock, enroll  # noqa: F401
from remote_kit import HTTPS, app, bearer, front, local, loop, passkey_store, remote, ungated  # noqa: F401
from test_dashboard_click import (  # noqa: F401
    HASH,
    PASS_ENV,
    REQ,
    agent_access,
    click_start,
    hub_join,
    rotate_body,
    script,
)
from webauthn_kit import Authenticator

from orch_dashboard import routegate, stepup
from orch_dashboard.auth import STEPUP_FRESH
from orch_dashboard.passkey_routes import CHALLENGE_TTL
from orch_dashboard.stepup import GUARDED_PATHS, STEP_UP

FEATURES = [
    "custody",
    "device_rotate",
    "runner_switch",
    "passkey_stepup",
    "sealed_passphrase",
    "backup_key",
    "reveal",
    "github_token",
]
CHALLENGE_KEY = "A" * 87
OTHER_HASH = "sha256:" + "cd" * 32
HUBF = {"hub_id": "hub_" + "a" * 26, "invite_sha256": "0" * 64}
HUB_CARD = '{"o":{"hub_id":"hub_' + "a" * 26 + '"}}'

#: (STEP_UP entry name, the action the ceremony is asked for, method, path template, body, the digest it binds to or
#: None for an action digest). Every variant of every entry, as a browser would send it.
CUSTODY_ON = ("custody.on", {"on": True, "passphrase": "correct horse"})
VARIANTS = [
    (STEP_UP[0].name, "custody", CUSTODY_ON[0], CUSTODY_ON[1], None),
    (STEP_UP[0].name, "custody", "custody.on", {"on": True, "resume": True}, None),
    (
        STEP_UP[1].name,
        "custody",
        "custody.off",
        {"on": False, "request_id": REQ, "view_digest": HASH, "sealed": {"custody.passphrase": PASS_ENV}},
        HASH,
    ),
    (STEP_UP[2].name, "rotate", "rotate", rotate_body(), HASH),
    (STEP_UP[3].name, "runner", "runner.on", {"on": True, "view_digest": HASH}, HASH),
    (STEP_UP[3].name, "runner", "runner.on", {"on": False, "terminals": True, "view_digest": HASH}, HASH),
    (
        STEP_UP[4].name,
        "sign",
        "sign",
        {"request_id": REQ, "view_digest": HASH, "sealed": {"hub.backup_key": PASS_ENV}},
        HASH,
    ),
    (STEP_UP[4].name, "sign", "sign", {"request_id": REQ, "view_digest": HASH, "reveal_key": CHALLENGE_KEY}, HASH),
    # #1198 C16: the GitHub token is a sealed secret like the others, so a remote session needs the touch
    (
        STEP_UP[5].name,
        "github",
        "github.token",
        {"request_id": REQ, "view_digest": HASH, "sealed": {"github.token": PASS_ENV}},
        HASH,
    ),
]
IDS = [f"{r}-{i}" for i, (_, r, _, _, _) in enumerate(VARIANTS)]


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def stub_world(front, app, remote, stub, script, auth, passkey_store):
    """One enrolled passkey, a remote session, and the faked signer able to check a passkey."""
    script.features = FEATURES
    authn, res = enroll(front, app)
    assert res.status_code == 200, res.text
    return type("W", (), {})() if False else _World(front, app, remote, stub, script, auth, authn)


class _World:
    def __init__(self, front, app, token, stub, script, auth, authn):
        self.front, self.app, self.token, self.stub, self.script, self.auth, self.authn = (
            front,
            app,
            token,
            stub,
            script,
            auth,
            authn,
        )

    def headers(self, token=None):
        return bearer(token or self.token, HTTPS)

    def options(self, action, digest=None, token=None, credential_id=None, **fields):
        host = action in ("device.add", "passkey.remove")
        path = "/api/stepup/options" if host else f"/api/w/{self.stub.ws}/stepup/options"
        body = {"action": action, **fields}
        if digest is not None:
            body["view_digest"] = digest
        if credential_id is not None:
            body["credential_id"] = credential_id
        if action in ("custody.off", "rotate", "github.token", "sign") and digest is not None:
            body.setdefault("request_id", REQ)
        res = self.front.post(path, json=body, headers=self.headers(token))
        if res.status_code == 200 and "link_id" in res.json():
            self.link_id = res.json()["link_id"]
        return res

    def verify(self, opts, token=None, authn=None, **mutate):
        got = (authn or self.authn).get(opts["publicKey"])
        got["response"] = {k: v for k, v in got["response"].items() if v is not None}
        body = {"challenge_id": opts["challenge_id"], **got, **mutate}
        return self.front.post("/api/stepup/verify", json=body, headers=self.headers(token))

    def touch(self, action, digest=None, token=None, credential_id=None, authn=None, **fields):
        """A whole step-up: options, the authenticator, verify. Returns the verify response."""
        opts = self.options(action, digest, token, credential_id, **fields)
        assert opts.status_code == 200, opts.text
        res = self.verify(opts.json(), token, authn)
        assert res.status_code == 200, res.text
        return res

    def post(self, route, body, token=None):
        return self.front.post(f"/api/w/{self.stub.ws}/click/{route}", json=body, headers=self.headers(token))

    def signer_calls(self):
        return [c for cl in self.script.clients for c in cl.calls if c[0] != "status"]


def refused_for_stepup(res):
    return res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"


def test_every_entry_of_the_list_has_a_variant_or_is_a_host_route():
    assert {name for name, *_ in VARIANTS} == {s.name for s in STEP_UP if s.base == stepup.CLICK}
    assert {s.action for s in STEP_UP} >= {a for _, _, a, _, _ in VARIANTS}


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_each_variant_is_refused_without_a_step_up_and_reaches_no_signer(stub_world, name, route, action, body, digest):
    w = stub_world
    assert refused_for_stepup(w.post(route, body)), name
    assert w.signer_calls() == [], "the signer must never be called"


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_each_variant_passes_with_a_fresh_matching_step_up_and_the_signer_gets_the_assertion(
    stub_world, name, route, action, body, digest
):
    w = stub_world
    w.touch(action, digest)
    res = w.post(route, body)
    assert not refused_for_stepup(res), res.text
    if route == "sign":  # no prepared request behind this body: `test_a_sign_...` below follows a real one
        return
    (call,) = [c for c in w.signer_calls() if c[0] != "stepup"]
    args = call[1]
    assert args["remote"] is True
    nonce = b64u(w.script.nonces[-1])
    assert args["stepup"]["nonce"] == nonce and args["stepup"]["id"] == b64u(w.authn.credential_id)
    assert set(args["stepup"]["response"]) >= {"clientDataJSON", "authenticatorData", "signature"}
    assert all(c[1] != {} for c in w.signer_calls())


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_a_step_up_is_used_once(stub_world, name, route, action, body, digest):
    w = stub_world
    w.touch(action, digest)
    assert not refused_for_stepup(w.post(route, body))
    assert refused_for_stepup(w.post(route, body)), "a replay"


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_a_stale_step_up_is_refused(stub_world, clock, name, route, action, body, digest):
    w = stub_world
    w.touch(action, digest)
    clock.now += STEPUP_FRESH
    assert refused_for_stepup(w.post(route, body))
    assert w.signer_calls() == [] or all(c[0] == "stepup" for c in w.signer_calls())


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_a_step_up_for_another_review_or_action_is_refused(stub_world, name, route, action, body, digest):
    w = stub_world
    if digest is not None:
        w.touch(action, OTHER_HASH)  # the right action, another review
    else:
        w.touch("custody.on" if action != "custody.on" else "hub.connect", **({} if action != "custody.on" else HUBF))
    assert refused_for_stepup(w.post(route, body))


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_a_step_up_of_another_session_is_refused(stub_world, auth, name, route, action, body, digest):
    w = stub_world
    other = auth.open_remote()["token"]
    w.touch(action, digest, token=other)
    assert refused_for_stepup(w.post(route, body)), "the first session never touched"
    assert not refused_for_stepup(w.post(route, body, token=other))


def connect_fields(body):
    import hashlib

    return {"hub_id": "hub_" + "a" * 26, "invite_sha256": hashlib.sha256(body["invite"].encode()).hexdigest()}


def test_the_hub_connect_is_a_step_up_of_its_own(stub_world):
    w = stub_world
    path = f"/api/w/{w.stub.ws}/hub/connect"
    body = {"hub_card": HUB_CARD, "invite": "i" * 40, "idem": "a_" + "0" * 26}
    assert refused_for_stepup(w.front.post(path, json=body, headers=w.headers()))
    other = {**body, "invite": "j" * 40}
    w.touch("hub.connect", **connect_fields(body))
    assert refused_for_stepup(w.front.post(path, json=other, headers=w.headers())), "another invite"
    w.touch("hub.connect", **connect_fields(body))
    res = w.front.post(path, json=body, headers=w.headers())
    assert not refused_for_stepup(res), res.text
    assert refused_for_stepup(w.front.post(path, json=body, headers=w.headers())), "a replay"


# -- the ceremony --------------------------------------------------------------------------------------------------


def test_the_challenge_is_bound_to_the_session_and_cannot_be_verified_by_another(stub_world, auth):
    w = stub_world
    other = auth.open_remote()["token"]
    opts = w.options("sign", HASH).json()
    assert w.verify(opts, token=other).status_code == 403
    # spent by the failed try: the owner of the session cannot use it either
    assert w.verify(opts).status_code == 403


def test_the_challenge_is_single_use_and_lives_120_seconds(stub_world, clock):
    w = stub_world
    opts = w.options("sign", HASH).json()
    assert w.verify(opts).status_code == 200
    assert w.verify(opts).status_code == 403
    opts = w.options("sign", HASH).json()
    clock.now += CHALLENGE_TTL
    assert w.verify(opts).status_code == 403


def test_the_challenge_is_the_stepup_challenge_of_the_signers_nonce_and_the_digest(stub_world):
    from orch.click import passkeys as wa

    w = stub_world
    opts = w.options("sign", HASH).json()
    assert opts["publicKey"]["challenge"] == b64u(wa.stepup_challenge(w.script.nonces[-1], HASH))
    assert opts["publicKey"]["userVerification"] == "required"
    assert [c["id"] for c in opts["publicKey"]["allowCredentials"]] == [b64u(w.authn.credential_id)]
    assert opts["view_digest"] == HASH


def test_an_assertion_over_another_challenge_or_origin_or_without_uv_is_refused(stub_world):
    w = stub_world
    for authn in (Authenticator(w.authn.rp_id, uv=False), Authenticator("other.example.com")):
        opts = w.options("sign", HASH).json()
        assert w.verify(opts, authn=authn).status_code == 403
    opts = w.options("sign", HASH).json()
    wrong = w.options("sign", OTHER_HASH).json()
    opts["publicKey"]["challenge"] = wrong["publicKey"]["challenge"]  # signed over the wrong challenge
    assert w.verify(opts).status_code == 403
    assert refused_for_stepup(w.post("sign", {"request_id": REQ, "view_digest": HASH, "reveal_key": CHALLENGE_KEY}))


def test_the_ceremony_takes_a_closed_list_and_the_right_fields(stub_world):
    w = stub_world
    path = f"/api/w/{w.stub.ws}/stepup/options"
    for body in (
        {"action": "custody.on", "view_digest": HASH},  # no review for this one
        {"action": "sign"},  # a review is needed
        {"action": "device.add"},  # a host action on the workspace route
        {"action": "sign", "view_digest": "sha256:zz"},
        {"action": "sign", "view_digest": HASH, "extra": 1},
    ):
        res = w.front.post(path, json=body, headers=w.headers())
        assert res.status_code == 400, (body, res.text)
    assert w.options("passkey.remove").status_code == 400  # needs the credential id
    assert w.options("device.add", credential_id="A" * 43).status_code == 400


def test_the_ceremony_needs_the_signer_to_know_passkeys(stub_world):
    w = stub_world
    w.script.features = ["custody", "runner_switch"]
    res = w.options("runner.on", HASH)
    assert res.status_code == 501 and res.json()["error"]["code"] == "click.contract"
    # and a remote session's call is not sent to a signer that would ignore the assertion
    assert w.post("custody", {"on": False}).status_code == 501
    assert w.signer_calls() == []
    assert w.options("hub.connect", **HUBF).status_code == 200  # the server's own ops need no signer feature


def test_the_ceremony_exists_only_on_the_remote_listener(stub_world, loop, local):
    w = stub_world
    for path, body in (
        (f"/api/w/{w.stub.ws}/stepup/options", {"action": "sign", "view_digest": HASH}),
        ("/api/stepup/options", {"action": "device.add"}),
        ("/api/stepup/verify", {}),
    ):
        res = loop.post(path, json=body, headers=bearer(local, "http://127.0.0.1:43210"))
        assert res.status_code in (400, 404), (path, res.text)
    assert loop.post(f"/api/w/{w.stub.ws}/stepup/options", json={"action": "sign", "view_digest": HASH},
                     headers=bearer(local, "http://127.0.0.1:43210")).status_code == 404  # fmt: skip


def test_a_bad_store_means_no_ceremony(stub_world):
    w = stub_world
    import os

    os.chmod(w.app.state.passkeys.store.path, 0o644)
    assert w.options("sign", HASH).status_code in (401, 403)


# -- local sessions, and the plain variants ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_a_local_session_needs_no_step_up_and_sends_no_remote_arguments(
    stub_world, loop, local, name, route, action, body, digest
):
    w = stub_world
    res = loop.post(f"/api/w/{w.stub.ws}/click/{route}", json=body, headers=bearer(local, "http://127.0.0.1:43210"))
    assert not refused_for_stepup(res), res.text
    for _, args in w.signer_calls():
        assert "remote" not in args and "stepup" not in args


def test_plain_variants_stay_plain_for_a_remote_session_but_say_remote_to_the_signer(stub_world):
    w = stub_world
    w.script.rotation = {"request_id": REQ, "view_digest": HASH}
    for route, body in (
        ("runner/review", {"on": False}),
        ("runner/review", {"on": True}),  # a review only opens the sheet: the touch is at `runner`
        ("runner", {"on": False, "view_digest": HASH}),
        ("custody", {"on": False}),
    ):
        res = w.post(route, body)
        assert res.status_code == 200, (route, res.text)
    calls = w.signer_calls()
    assert calls and all("stepup" not in args for _, args in calls), calls
    assert [args.get("remote") for op, args in calls if op in ("custody_off", "runner") and "review" not in args] == [
        True,
        True,
    ]


def test_the_reviews_of_the_step_up_ops_are_plain(stub_world):
    w = stub_world
    for route in ("custody/seal", "rotate/seal"):
        res = w.post(route, {})
        assert not refused_for_stepup(res), (route, res.text)
    assert not refused_for_stepup(w.post("prepare", agent_access()))
    assert not refused_for_stepup(w.post("prepare", hub_join()))
    assert not refused_for_stepup(w.post("prepare", {"action": "hub_leave"}))


# -- the guards ----------------------------------------------------------------------------------------------------


def first_statement(endpoint):
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    body = tree.body[0].body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):  # the docstring
        body = body[1:]
    return body[0]


def route_of(step, app):
    method = "DELETE" if step.route.startswith("passkeys/{") else "POST"
    (handler,) = [r for r in app.routes if isinstance(r, APIRoute) and r.path == step.path and method in r.methods]
    return handler


@pytest.mark.parametrize("path", GUARDED_PATHS)
def test_every_handler_of_the_list_calls_the_helper_first(app, path):
    """Fails when a handler of the list forgets `refuse_step_up(...)` or puts something before it."""
    (step,) = [s for s in STEP_UP if s.path == path][:1]
    stmt = first_statement(route_of(step, app).endpoint)
    call = stmt.value
    if isinstance(stmt, ast.Assign):  # `step = refuse_step_up(...)`
        call = stmt.value
    else:
        assert isinstance(stmt, ast.Expr), (path, ast.dump(stmt)[:80])
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "refuse_step_up", path
    assert isinstance(call.args[2], ast.Constant) and call.args[2].value == step.route


def test_the_helper_lets_a_local_session_through_with_nothing_to_add():
    class Auth:
        def login_kind(self, token):
            return "local"

    class Request:
        class state:
            token = "t"

    body = type("B", (), {"on": True, "sealed": None, "view_digest": None})()
    assert stepup.refuse_step_up(Auth(), Request, "custody", body, "ws") == {}


def test_an_unknown_session_gets_no_step_up(app):
    class Request:
        class state:
            token = "nobody"

    body = type("B", (), {"on": True, "sealed": None, "view_digest": None})()
    with pytest.raises(stepup.HostError) as caught:
        stepup.refuse_step_up(app.state.auth, Request, "custody", body, "ws")
    assert caught.value.error["code"] == "passkey.stepup_required"


def test_the_whole_route_entries_are_exactly_the_step_up_rules_of_the_table():
    """The route table lets a STEP_UP route through to its handler, which asks; this keeps the two lists in step."""
    in_table = {path for (_, path), (rule, _) in routegate.TABLE.items() if rule == routegate.STEP_UP}
    always = {s.path for s in STEP_UP if s.applies is stepup.always}
    assert in_table == always


def test_the_body_decides_routes_of_the_table_are_exactly_the_guarded_click_routes():
    """A new body-decides handler cannot go unguarded: the table marks it, and so must `stepup.STEP_UP` (and the
    reverse)."""
    from orch_dashboard.routegate import ALLOWED, TABLE

    body_decides = {
        path
        for (method, path), (rule, why) in TABLE.items()
        if rule == ALLOWED and why.startswith("body decides") and method == "POST"
    }
    assert body_decides
    whole_route = {path for (_, path), (rule, _) in TABLE.items() if rule == routegate.STEP_UP}
    guarded = set(GUARDED_PATHS)
    assert body_decides == guarded - whole_route
    assert whole_route <= guarded


def test_no_step_up_name_of_the_owner_list_is_missing():
    """Owner decision of 24 Oct 2026 (host spec 10.5): One click on, hub join and mode, device rotate, a grant whose
    secret is revealed, dashboard execution on, adding a passkey or device, removing a passkey."""
    assert {s.action for s in STEP_UP} == {
        "custody.on",
        "custody.off",
        "rotate",
        "github.token",  # #1198 C16: not on the owner's list; added because the sealed GitHub token is a secret
        "runner.on",
        "sign",
        "hub.connect",
        "device.add",
        "passkey.remove",
    }
    assert stepup.SIGNER_ACTIONS | {"hub.connect", "device.add", "passkey.remove"} == {s.action for s in STEP_UP}


def test_a_real_sign_carries_the_assertion_to_the_signer_and_a_replay_never_does(stub_world, monkeypatch):
    """A hub join prepared and signed from a remote session: the review is plain, the sign needs the touch, and the
    signer gets the very assertion the browser made."""
    import test_dashboard_click as t

    w = stub_world
    check = t.HostCheck()
    monkeypatch.setattr(w.app.state, "human", check.human)
    monkeypatch.setattr("orch_dashboard.click.verify", check.request)
    monkeypatch.setattr(t, "GRANT_WS", w.script.ws)
    check.verified = {"fingerprint": "0123456789ab"}
    w.script.view = t.backup_view()
    prepared = w.post("prepare", t.hub_join())
    assert prepared.status_code == 200, prepared.text
    rid = prepared.json()["request_id"]
    body = {"request_id": rid, "view_digest": HASH, "sealed": {"hub.backup_key": t.BACKUP_ENV}}
    assert refused_for_stepup(w.post("sign", body))
    assert t.sent_of(w.script, "sign") == []
    w.touch("sign", HASH)
    res = w.post("sign", body)
    assert res.status_code == 200, res.text
    (call,) = t.sent_of(w.script, "sign")
    assert call[1]["remote"] is True and call[1]["stepup"]["nonce"] == b64u(w.script.nonces[-1])
    assert call[1]["request_id"] == rid and "sealed" in call[1]
    assert refused_for_stepup(w.post("sign", body))
    assert len(t.sent_of(w.script, "sign")) == 1


def test_the_proof_the_server_relays_is_what_the_real_signer_check_accepts(stub_world):
    """The server's relayed assertion and core's `Stepups.check` agree on the format, with the real signer code."""
    import time

    from orch.click.stepup import Stepups
    from remote_kit import NAME

    w = stub_world
    w.touch("runner.on", HASH)
    assert w.post("runner", {"on": True, "view_digest": HASH}).status_code == 200
    (proof,) = [a["stepup"] for op, a in w.signer_calls() if op == "runner"]
    steps = Stepups(w.app.state.passkeys.store.path.parent, time.monotonic, rp_id=lambda: NAME,
                    store=w.app.state.passkeys.store)  # fmt: skip
    (asked,) = [a for op, a in w.signer_calls() if op == "stepup"]
    assert asked == {"workspace_id": w.stub.ws, "view_digest": HASH, "op": "runner"}
    steps.nonces[w.script.nonces[-1]] = (w.stub.ws, HASH, "runner", None, time.monotonic())  # the signer issued it
    steps.check(w.stub.ws, HASH, "runner", None, proof)  # does not raise
    with pytest.raises(Exception, match="passkey.stepup_required"):
        steps.check(w.stub.ws, HASH, "runner", None, proof)  # used up


@pytest.mark.parametrize(("name", "route", "action", "body", "digest"), VARIANTS, ids=IDS)
def test_the_servers_list_and_the_signers_list_agree_on_what_needs_a_step_up(
    stub_world, name, route, action, body, digest
):
    """What each handler sends to the signer is a step-up variant for the signer too (and for the same digest)."""
    from orch.click.signer import ClickSigner

    w = stub_world
    if route == "sign":  # no prepared request behind these bodies: asked directly with what the handler would send
        args = {"workspace_id": w.stub.ws, "request_id": body["request_id"], "view_digest": body["view_digest"]}
        args |= {k: body[k] for k in ("sealed", "reveal_key") if k in body}
        got = ClickSigner._stepup_target("sign", args)
        assert got is not None and got[1:3] == (HASH, "sign")
        return
    w.touch(action, digest)
    assert not refused_for_stepup(w.post(route, body))
    (call,) = [c for c in w.signer_calls() if c[0] != "stepup"]
    args = {k: v for k, v in call[1].items() if k not in ("remote", "stepup")}
    got = ClickSigner._stepup_target(call[0], args)
    assert got is not None, call
    if digest is not None:
        assert got[1] == digest
    else:
        from orch.click.passkeys import action_digest

        assert got[1] == action_digest("custody.on", w.stub.ws)


def test_the_plain_variants_the_server_sends_are_plain_for_the_signer_too(stub_world):
    from orch.click.signer import ClickSigner

    w = stub_world
    for route, body in (
        ("runner/review", {"on": True}),
        ("runner", {"on": False, "view_digest": HASH}),
        ("custody", {"on": False}),
        ("custody/seal", {}),
    ):
        w.post(route, body)
    sent = [(op, a) for op, a in w.signer_calls() if op != "status"]
    assert sent
    for op, args in sent:
        plain = {k: v for k, v in args.items() if k not in ("remote", "stepup")}
        assert ClickSigner._stepup_target(op, plain) is None, (op, args)


def test_a_remote_session_always_says_remote_even_for_a_plain_variant(stub_world):
    w = stub_world
    w.post("runner", {"on": False, "view_digest": HASH})
    (call,) = [a for op, a in w.signer_calls() if op == "runner"]
    assert call["remote"] is True and "stepup" not in call


def test_a_held_step_up_without_a_proof_never_passes_for_a_signer_op(stub_world):
    """The structural case: a server-only grant (no proof) for the same digest cannot satisfy a signer op, and the
    grants are keyed by action, so another action's touch for the same digest does not either."""
    w = stub_world
    token = w.token
    w.auth.stepup_grant(token, "runner.on", HASH, None)  # a grant with no proof
    assert refused_for_stepup(w.post("runner", {"on": True, "view_digest": HASH}))
    w.touch("rotate", HASH)  # same digest, another action
    assert refused_for_stepup(w.post("runner", {"on": True, "view_digest": HASH}))
    assert w.signer_calls() == [c for c in w.signer_calls() if c[0] == "stepup"]
