"""H5c click-to-sign, the server side: the signer child's lifecycle, the four typed routes, error mapping and the
rules the section 0.8.6 item 7 puts on this process. The signer is faked here (a scripted channel); the real signer
runs in test_realhost_click.py."""

import logging
import threading
import time
from pathlib import Path

import pytest
from conftest import ORIGIN
from orch.click.protocol import ClickError

from orch_dashboard import click_signer

IDEM = "a_{:0>26}"
HASH = "sha256:" + "ab" * 32
REQ = "hr_" + "0" * 25 + "1"
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
CANARY = "CANARY-LINE-never-in-a-log"


def idem(n):
    return IDEM.format(n)


class Fake:
    """A scripted signer channel: records every call; `handler(op, args)` answers or raises ClickError."""

    def __init__(self, handler):
        self.handler, self.calls, self.closed, self.proc = handler, [], False, None

    def call(self, op, args, timeout=None):
        self.calls.append((op, args))
        return self.handler(op, args)

    def close(self):
        self.closed = True


class Script:
    def __init__(self, ws):
        self.ws, self.enabled, self.available, self.contract = ws, True, True, 1
        self.refuse: dict[str, ClickError] = {}
        self.view = {"view": {"risk": RISK, "lines": [CANARY], "op": "approve"}, "view_digest": HASH}
        self.prepared = 0
        self.delay = 0.0
        self.started: list[dict] = []
        self.clients: list[Fake] = []

    def handler(self, op, args):
        if op in self.refuse:
            raise self.refuse[op]
        if op == "status":
            row = {
                "workspace_id": self.ws,
                "prefix": "DEMO",
                "custody": "convenience",
                "device": "d_1",
                "enabled": self.enabled,
                "available": self.available,
            }
            if not self.available:
                row["problem"] = "disabled"
            return {"core": "2.0.0.dev0", "contract": self.contract, "workspaces": [row]}
        if op == "prepare":
            time.sleep(self.delay)
            self.prepared += 1
            return {
                "request_id": f"hr_{self.prepared:026d}",
                "expires_at": "2026-10-12T12:10:00Z",
                "op": args["op"],
                "confirm": "click",
            }
        if op == "view":
            return self.view
        if op == "sign":
            return {"result": {"data": {"secret": "no"}, "key": "DEMO-0001", "seq": 7}, "duplicate": False}
        if op == "cancel":
            return {"state": "cancelled"}
        raise AssertionError(op)

    def start(self, env):
        self.started.append(env)
        client = Fake(self.handler)
        self.clients.append(client)
        return client


@pytest.fixture
def script(stub):
    return Script(stub.ws)


@pytest.fixture
def click_start(script, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)
    return script.start


class HostCheck:
    """The host's own record of a click request, as the server reads it with `human.get` through its own link, and
    what `adapters.handoff.request` was asked to verify (the hand-off's gate). The faked signer's request ids do not
    exist on the stub host, so the record is scripted; the real-host tests read the real one."""

    def __init__(self):
        self.reads, self.checks, self.cancelled, self.pending = [], [], [], {}
        self.mismatch = False

    def human(self, ws, op, args):
        if op == "human.get":
            self.reads.append(args)
            return {"op": "x"}
        if op == "human.cancel":
            self.cancelled.append(args["request_id"])
            return {"state": "cancelled"}
        if op == "human.pending":
            return {"requests": [{"id": args["request_id"], "state": self.pending.get(args["request_id"], "bound")}]}
        raise AssertionError(op)

    def request(self, prepared, ws, **kw):
        self.checks.append(kw)
        if self.mismatch:
            from orch_dashboard.errors import fail

            raise fail("internal", "x")
        return {}


@pytest.fixture(autouse=True)
def host_check(client, monkeypatch):
    check = HostCheck()
    monkeypatch.setattr(client.app.state, "human", check.human)
    monkeypatch.setattr("orch_dashboard.click.verify", check.request)
    return check


def hdr(token, write=True):
    return {"Authorization": f"Bearer {token}", **({"Origin": ORIGIN} if write else {})}


def url(stub, step):
    return f"/api/w/{stub.ws}/click/{step}"


def approve(n=1):
    return {"action": "approve", "key": "DEMO-0001", "gate": "requirements", "idem": idem(n)}


def prepare(client, token, stub, body=None):
    return client.post(url(stub, "prepare"), json=body or approve(), headers=hdr(token))


def test_the_signer_is_one_child_started_on_first_use_with_a_clean_environment(client, token, stub, script, gateway):
    assert script.started == []  # nothing starts at boot
    got = client.get("/api/workspaces", headers=hdr(token, False)).json()
    assert len(script.started) == 1
    client.get("/api/workspaces", headers=hdr(token, False))
    assert len(script.started) == 1  # one per server, not one per request
    env = script.started[0]
    assert env["ORCH_STATE_DIR"] == str(gateway.state) and "PYTHONPATH" not in env
    assert "click" in got[0]["features"]
    assert got[0]["click"] == {"custody": "convenience", "enabled": True, "available": True}


def test_pythonpath_is_not_passed_to_the_signer(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else/core")
    assert "PYTHONPATH" not in click_signer.child_env(tmp_path)


@pytest.mark.parametrize(
    ("enabled", "available"),
    [(False, False), (True, False)],
)
def test_no_click_feature_unless_the_flag_is_on_and_the_signer_can_sign(
    client, token, stub, script, enabled, available
):
    script.enabled, script.available = enabled, available
    ws = client.get("/api/workspaces", headers=hdr(token, False)).json()[0]
    assert "click" not in ws["features"]
    assert ws["click"]["enabled"] is enabled and ws["click"]["available"] is False
    assert ws["click"]["problem"] == "disabled"


def test_a_signer_of_another_contract_is_refused(client, token, stub, script):
    script.contract = 2
    ws = client.get("/api/workspaces", headers=hdr(token, False)).json()[0]
    assert "click" not in ws["features"] and ws["click"] is None
    assert script.clients[0].closed  # that child is stopped
    res = prepare(client, token, stub)
    assert res.status_code == 501 and res.json()["error"]["code"] == "click.contract"
    assert "orch-core" in res.json()["error"]["message"]
    assert [c for c in script.clients[0].calls if c[0] == "prepare"] == []


def test_prepare_chooses_the_op_here_and_binds_the_idem_to_the_session(client, token, stub, script, auth):
    res = prepare(client, token, stub)
    assert res.status_code == 200, res.text
    assert res.json() == {"request_id": REQ, "expires_at": "2026-10-12T12:10:00Z", "op": "approve", "confirm": "click"}
    op, args = [c for c in script.clients[0].calls if c[0] == "prepare"][0]
    assert args["workspace_id"] == stub.ws and args["op"] == "approve"
    assert args["args"] == {"ref": "DEMO-0001", "gate": "requirements"}
    assert args["idem"].startswith("a_") and len(args["idem"]) == 28 and args["idem"] != idem(1)
    again = prepare(client, token, stub)  # the same click retried: the same key
    assert [c for c in script.clients[0].calls if c[0] == "prepare"][1][1]["idem"] == args["idem"]
    assert again.status_code == 200
    other = client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()
    prepare(client, other["token"], stub)
    assert [c for c in script.clients[0].calls if c[0] == "prepare"][2][1]["idem"] != args["idem"]


@pytest.mark.parametrize(
    "extra",
    [
        {"op": "grant"},
        {"op": "approve"},
        {"args": {"x": 1}},
        {"files": {"a": {"b64": "AA=="}}},
        {"workspace_id": "f" * 32},
    ],
)
def test_the_browser_cannot_name_an_op_or_arguments(client, token, stub, script, extra):
    res = prepare(client, token, stub, {**approve(), **extra})
    assert res.status_code == 400
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_grant_is_not_an_action_and_stays_in_the_terminal(client, token, stub, script):
    for action in ("grant", "grant_issue", "apply"):
        res = prepare(client, token, stub, {"action": action, "idem": idem(2)})
        assert res.status_code == 400
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_a_member_add_sends_the_certificate_as_a_file(client, token, stub, script):
    import base64
    import json

    pk = base64.urlsafe_b64encode(b"\x04" + b"a" * 64).rstrip(b"=").decode()
    cert = json.dumps({"v": 1})
    body = {"action": "member_add", "name": "Mara", "role": "member", "pk": pk, "cert": cert, "idem": idem(3)}
    res = prepare(client, token, stub, body)
    assert res.status_code == 200, res.text
    args = [c for c in script.clients[0].calls if c[0] == "prepare"][0][1]
    assert args["op"] == "member.add" and args["args"]["cert"] == "-"
    assert base64.b64decode(args["files"]["cert"]["b64"]) == cert.encode()


def test_view_is_the_signers_view_unchanged(client, token, stub, script):
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200
    assert res.json() == script.view  # nothing added, hidden or re-rendered
    assert res.headers["cache-control"] == "no-store"
    call = [c for c in script.clients[0].calls if c[0] == "view"][0]
    assert call[1] == {"workspace_id": stub.ws, "request_id": REQ}


def test_sign_sends_exactly_what_was_typed_and_answers_without_the_hosts_result(client, token, stub, script):
    prepare(client, token, stub)
    body = {"request_id": REQ, "view_digest": HASH, "word": "  DEMO~ab  "}
    res = client.post(url(stub, "sign"), json=body, headers=hdr(token))
    assert res.status_code == 200
    assert res.json() == {"state": "submitted", "duplicate": False, "key": "DEMO-0001", "seq": 7}
    sent = [c for c in script.clients[0].calls if c[0] == "sign"][0][1]
    assert sent == {"workspace_id": stub.ws, "request_id": REQ, "view_digest": HASH, "word": "  DEMO~ab  "}
    plain = {"request_id": "hr_" + "0" * 25 + "2", "view_digest": HASH}
    prepare(client, token, stub, approve(2))
    client.post(url(stub, "sign"), json=plain, headers=hdr(token))
    assert "word" not in [c for c in script.clients[0].calls if c[0] == "sign"][1][1]


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": REQ, "view_digest": "sha256:short"},
        {"request_id": REQ, "view_digest": HASH, "word": ""},
        {"request_id": REQ, "view_digest": HASH, "word": "x" * 129},
        {"request_id": "not-a-request", "view_digest": HASH},
        {"request_id": REQ, "view_digest": HASH, "extra": 1},
    ],
)
def test_sign_bodies_are_checked(client, token, stub, script, body):
    prepare(client, token, stub)
    assert client.post(url(stub, "sign"), json=body, headers=hdr(token)).status_code == 400
    assert [c for c in script.clients[0].calls if c[0] == "sign"] == []


def test_a_request_of_another_session_looks_like_none(client, token, stub, script, auth):
    prepare(client, token, stub)
    other = client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()
    for step, body in (
        ("view", {"request_id": REQ}),
        ("sign", {"request_id": REQ, "view_digest": HASH}),
        ("cancel", {"request_id": REQ}),
    ):
        res = client.post(url(stub, step), json=body, headers=hdr(other["token"]))
        assert res.status_code == 404, step
    assert [c for c in script.clients[0].calls if c[0] in ("view", "sign", "cancel")] == []
    assert (
        client.post(url(stub, "view"), json={"request_id": "hr_" + "0" * 25 + "2"}, headers=hdr(token)).status_code
        == 404
    )


def test_cancel_cancels_and_forgets(client, token, stub, script):
    prepare(client, token, stub)
    res = client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token))
    assert res.json() == {"id": REQ, "state": "cancelled"}
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404


def test_logout_cancels_what_the_session_left_open(client, token, stub, script):
    prepare(client, token, stub)
    client.post("/auth/logout", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    assert [c[1]["request_id"] for c in script.clients[0].calls if c[0] == "cancel"] == [REQ]


@pytest.mark.parametrize(
    ("error", "http"),
    [
        (ClickError("sign.convenience_unavailable", "disabled: off", reason="disabled"), 409),
        (ClickError("sign.convenience_unavailable", "other_member: x", reason="other_member"), 409),
        (ClickError("sign.convenience_readers", "update the readers: relay"), 409),
        (ClickError("rate.limited", "slow down", retry_after_ms=4200, retryable=True), 503),
        (ClickError("custody.no_key", "no key"), 409),
        (ClickError("custody.unavailable", "locked"), 409),
        (ClickError("host.version_mismatch", "differs"), 501),
        (ClickError("host.unavailable", "gone"), 503),
        (ClickError("human.request_expired", "late"), 410),
        (ClickError("human.request_state", "submitted"), 409),
        (ClickError("human.click_refused", "short", reason="too_soon"), 409),
        (ClickError("human.click_refused", "short", reason="view_changed"), 409),
        (ClickError("human.click_refused", "no", reason="terminal_only"), 409),
    ],
)
def test_every_refusal_keeps_its_code_reason_and_wait(client, token, stub, script, error, http):
    prepare(client, token, stub)
    script.refuse["sign"] = error
    res = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    assert res.status_code == http
    got = res.json()["error"]
    assert got["code"] == error.code and got.get("reason") == error.reason
    assert got.get("retry_after_ms") == error.retry_after_ms
    # A refusal never cancels: the request stays this session's, to try again or to cancel itself. (One that can
    # never be signed again, because it ended, is forgotten and frees its quota slot.)
    ended = (error.reason or error.code) in {
        "expired",
        "not_prepared_here",
        "custody_changed",
        "human.request_expired",
        "human.request_state",
    }
    seen = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code
    assert seen == (404 if ended else 200)
    assert [c for c in script.clients[0].calls if c[0] == "cancel"] == []


def test_a_signer_that_exits_is_restarted_and_open_approvals_are_gone(client, token, stub, script, gateway):
    clock = [100.0]
    gateway.click.clock = lambda: clock[0]
    prepare(client, token, stub)
    first = script.clients[0]

    class Dead:
        def poll(self):
            return 1

    first.proc = Dead()
    clock[0] += 5
    script.refuse["view"] = ClickError("human.click_refused", "not here", reason="not_prepared_here")
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert len(script.started) == 2 and first.closed
    assert res.status_code == 409 and res.json()["error"]["reason"] == "not_prepared_here"  # terminal-only now
    assert gateway.click.restarts == 1


def test_a_lost_channel_says_so_and_the_next_call_starts_a_new_child(client, token, stub, script, gateway):
    clock = [100.0]
    gateway.click.clock = lambda: clock[0]
    prepare(client, token, stub)

    def broken(op, args):
        raise BrokenPipeError

    script.clients[0].handler = broken
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 503 and res.json()["error"]["code"] == "click.unavailable"
    assert res.json()["error"]["retryable"] is True
    clock[0] += 5
    assert prepare(client, token, stub).status_code == 200
    assert len(script.started) == 2


def test_a_signer_that_cannot_start_is_unavailable_not_a_crash(client, token, stub, script, gateway):
    def boom(env):
        raise OSError("no python")

    gateway.click._start = boom
    res = prepare(client, token, stub)
    assert res.status_code == 503 and res.json()["error"]["code"] == "click.unavailable"
    assert "no python" not in res.text


def test_the_signer_is_stopped_with_the_server(client, token, stub, script, gateway):
    prepare(client, token, stub)
    gateway.close()
    assert script.clients[0].closed


def test_views_words_and_credentials_are_never_logged(client, token, stub, script, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    prepare(client, token, stub)
    client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "word": "TYPED-WORD"}, headers=hdr(token)
    )
    out = capfd.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert CANARY not in text and "TYPED-WORD" not in text and token not in text


def test_the_sign_routes_take_small_bodies(client, token, stub, script):
    prepare(client, token, stub)
    big = {"request_id": REQ, "view_digest": HASH, "word": "x" * 20000}
    assert client.post(url(stub, "sign"), json=big, headers=hdr(token)).status_code == 413


def test_the_sign_routes_need_the_session_and_a_same_origin_post(client, token, stub, script):
    body = {"request_id": REQ}
    assert client.post(url(stub, "view"), json=body).status_code == 401
    assert client.post(url(stub, "view"), json=body, headers=hdr(token, False)).status_code == 403  # no Origin
    bad = {**hdr(token, False), "Origin": "null"}
    assert client.post(url(stub, "view"), json=body, headers=bad).status_code == 403
    cross = {**hdr(token), "Sec-Fetch-Site": "cross-site"}
    assert client.post(url(stub, "view"), json=body, headers=cross).status_code == 403
    assert client.get(url(stub, "view"), headers=hdr(token, False)).status_code in (404, 405)  # no read route


def test_no_route_but_the_click_routes_calls_the_signer():
    """No addon path reaches `prepare`: only click.py asks the signer for anything but its status."""
    root = Path(click_signer.__file__).parent
    asking = sorted(p.name for p in root.rglob("*.py") if 'call("prepare"' in p.read_text())
    assert asking == ["click.py"]
    holders = sorted(
        p.name for p in root.rglob("*.py") if "gateway.click" in p.read_text() or "self.click" in p.read_text()
    )
    assert holders == ["click.py", "gateway.py"]  # the gateway reads `status` for the card; nothing else
    assert "orch.click" not in (root / "handoff.py").read_text()


def test_the_status_route_is_the_card_only(client, token, stub, script):
    res = client.get(url(stub, "status"), headers=hdr(token, False))
    assert res.json() == {"custody": "convenience", "enabled": True, "available": True}
    script.available = False
    assert client.get(url(stub, "status"), headers=hdr(token, False)).json()["problem"] == "disabled"


# -- parity with the hand-off: every gate of its routes, on the click routes too -------------------------------------


def comment(n):
    return {"action": "comment", "key": "DEMO-0001", "message": "hello", "idem": idem(n)}


def policy_body(n):
    policy = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
    return {"action": "policy_set", "gate": "requirements", "policy": policy, "idem": idem(n)}


def families(stub):
    """(name, prepare url, request-scoped calls: (method, url-for-an-id) per step)."""
    hand = f"/api/w/{stub.ws}/handoff"
    click = f"/api/w/{stub.ws}/click"
    return {
        "handoff": {"prepare": hand, "steps": [("get", lambda r: f"{hand}/{r}"), ("delete", lambda r: f"{hand}/{r}")]},
        "click": {
            "prepare": f"{click}/prepare",
            "steps": [("post", lambda r: f"{click}/view"), ("post", lambda r: f"{click}/cancel")],
        },
    }


@pytest.fixture(params=["handoff", "click"])
def family(request, stub, script, rich):
    return request.param, families(stub)[request.param]


def send(client, method, url, token=None, body=None, **headers):
    h = {**({"Authorization": f"Bearer {token}"} if token else {}), **headers}
    return client.request(method, url, json=body, headers=h)


def mutate(client, method, url, token, body=None, **headers):
    return send(client, method, url, token, body, **{"Origin": ORIGIN, **headers})


def code(res):
    return res.json().get("error", {}).get("code")


def test_parity_the_same_refusals_before_anything_is_prepared(client, token, stub, script, family):
    name, f = family
    ok = approve(900)
    assert send(client, "post", f["prepare"], None, ok, Origin=ORIGIN).status_code == 401
    assert send(client, "post", f["prepare"], token, ok).status_code == 403  # no Origin
    assert send(client, "post", f["prepare"], token, ok, Origin="null").status_code == 403
    assert mutate(client, "post", f["prepare"], token, ok, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert mutate(client, "post", f["prepare"], token, {**ok, "op": "grant"}).status_code == 400  # closed models
    assert mutate(client, "post", f["prepare"], token, {**ok, "key": "bad key"}).status_code == 400
    assert mutate(client, "post", f["prepare"], token, {"action": "grant", "idem": idem(901)}).status_code == 400
    assert mutate(client, "post", f["prepare"], token, {**ok, "idem": "not-an-idem"}).status_code == 400
    elsewhere = f["prepare"].replace(stub.ws, "f" * 32)
    assert mutate(client, "post", elsewhere, token, ok).status_code == 404  # an unpinned workspace
    served = mutate(client, "post", f["prepare"], token, policy_body(902))
    assert served.status_code in (200, 501), (name, served.text)  # same rule: the host must list the op in welcome.ops


def test_parity_a_workspace_without_the_addon_is_refused_everywhere(
    client, token, stub, script, family, gateway, monkeypatch
):
    name, f = family
    from orch_dashboard.errors import fail

    def off(ws):
        raise fail("addon.disabled", exit=3)

    monkeypatch.setattr(gateway, "enabled", off)
    assert code(mutate(client, "post", f["prepare"], token, approve(903))) == "addon.disabled"
    rid = "hr_" + "0" * 25 + "1"
    for method, build in f["steps"]:
        body = {"request_id": rid} if method == "post" else None
        res = (
            mutate(client, method, build(rid), token, body)
            if method != "get"
            else send(client, "get", build(rid), token)
        )
        assert code(res) in ("addon.disabled", "not_found"), (name, method, res.text)


def test_parity_other_sessions_requests_look_like_none(client, token, stub, script, family, auth):
    name, f = family
    made = mutate(client, "post", f["prepare"], token, approve(904))
    assert made.status_code == 200, made.text
    rid = made.json().get("id") or made.json()["request_id"]
    other = client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()[
        "token"
    ]
    for method, build in f["steps"]:
        body = {"request_id": rid} if method == "post" else None
        res = (
            mutate(client, method, build(rid), other, body)
            if method != "get"
            else send(client, "get", build(rid), other)
        )
        assert res.status_code == 404, (name, method, res.text)
    unknown = "hr_" + "0" * 24 + "77"
    for method, build in f["steps"]:
        body = {"request_id": unknown} if method == "post" else None
        res = (
            mutate(client, method, build(unknown), token, body)
            if method != "get"
            else send(client, "get", build(unknown), token)
        )
        assert res.status_code == 404, (name, method, res.text)


def test_parity_the_per_session_quota(client, token, stub, script, family):
    name, f = family
    for n in range(8):
        res = mutate(client, "post", f["prepare"], token, comment(910 + n))
        assert res.status_code == 200, (name, n, res.text)
    ninth = mutate(client, "post", f["prepare"], token, comment(930))
    assert ninth.status_code == 503 and code(ninth) == "quota.host", (name, ninth.text)


def test_parity_a_key_used_for_another_action_is_a_conflict(client, token, stub, script, family):
    name, f = family
    assert mutate(client, "post", f["prepare"], token, comment(940)).status_code == 200
    other = mutate(client, "post", f["prepare"], token, {**comment(940), "message": "another"})
    assert code(other) == "idem.conflict", (name, other.text)
    assert mutate(client, "post", f["prepare"], token, comment(940)).status_code == 200  # the same click again


def test_a_view_for_another_op_than_the_one_asked_for_is_refused(client, token, stub, script):
    prepare(client, token, stub)
    script.view = {"view": {**script.view["view"], "op": "close"}, "view_digest": HASH}
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_concurrent_prepares_cannot_overshoot_the_one_quota(client, token, stub, script, rich):
    """Click and hand-off requests share the session's quota; check and reserve are one step."""
    script.delay = 0.05  # a wide window between "checked" and "committed"
    results = []

    def go(n):
        if n % 2:
            res = client.post(f"/api/w/{stub.ws}/handoff", json=comment(1000 + n), headers=hdr(token))
        else:
            res = client.post(url(stub, "prepare"), json=comment(1000 + n), headers=hdr(token))
        results.append((n % 2, res.status_code, res.json().get("error", {}).get("code")))

    threads = [threading.Thread(target=go, args=(n,)) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    ok = [r for r in results if r[1] == 200]
    assert len(results) == 24 and len(ok) == 8, results  # exactly the quota, never more
    assert {r[2] for r in results if r[1] != 200} == {"quota.host"}


def test_a_failed_prepare_gives_its_slot_back(client, token, stub, script):
    script.refuse["prepare"] = ClickError("custody.no_key", "no key")
    for n in range(12):
        assert client.post(url(stub, "prepare"), json=comment(3000 + n), headers=hdr(token)).status_code == 409
    script.refuse.clear()
    assert client.post(url(stub, "prepare"), json=comment(3100), headers=hdr(token)).status_code == 200


# -- Opus review round (M1, M2, L3, L4, L5) ------------------------------------------------------------------------


class Alive:
    def poll(self):
        return None


def test_m1_a_lost_channel_during_sign_is_an_unknown_outcome_not_a_gone_helper(client, token, stub, script, host_check):
    prepare(client, token, stub)

    def timeout(op, args):
        if op == "sign":
            raise TimeoutError
        return script.handler(op, args)

    script.clients[0].handler = timeout
    script.clients[0].proc = Alive()
    res = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    err = res.json()["error"]
    assert (err["code"], err["reason"]) == ("human.click_refused", "uncertain")
    assert "terminal" not in err["message"].lower() and "gone" not in err["message"].lower()
    assert not script.clients[0].closed  # a live signer is not killed while a submit may be finishing
    # The request and its slot stay: it settles at the host, and the server reports where it stands (our own link).
    host_check.pending[REQ] = "bound"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "bound"
    host_check.pending[REQ] = "submitted"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "submitted"
    assert (
        client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404
    )  # settled: forgotten


def test_m1_a_signer_that_really_exited_is_dropped_and_restarted(client, token, stub, script, gateway):
    clock = [100.0]
    gateway.click.clock = lambda: clock[0]
    prepare(client, token, stub)

    script.clients[0].proc = Alive()  # a broken pipe is a lost channel whatever the process says

    def broken(op, args):
        raise BrokenPipeError

    script.clients[0].handler = broken
    res = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    assert res.json()["error"].get("reason") == "uncertain", res.text  # still never "sign in the terminal" for a sign
    assert script.clients[0].closed
    clock[0] += 5
    assert prepare(client, token, stub, {**comment(7), "message": "after"}).status_code == 200
    assert len(script.started) == 2


def test_m2_the_view_is_checked_against_the_hosts_own_record(client, token, stub, script, host_check):
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200
    assert host_check.reads == [{"request_id": REQ}]
    kw = host_check.checks[0]
    assert kw["expect_op"] == "approve" and kw["expect_key"] == "DEMO-0001" and kw["expect_events"] is None
    # A settings change carries the exact event it must prepare.
    from orch_dashboard.handoff import SettingsSet

    body = {"action": "settings_set", "grant_hours": 12, "idem": idem(5)}
    assert SettingsSet(**body)
    script.view = {"view": {**script.view["view"], "op": "settings.set"}, "view_digest": HASH}


def test_m2_a_host_that_prepared_something_else_is_refused_cancelled_and_never_shown(
    client, token, stub, script, host_check
):
    prepare(client, token, stub)
    host_check.mismatch = True
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"
    assert [c for c in script.clients[0].calls if c[0] == "view"] == []  # the signer's View was never asked for
    assert [c for c in script.clients[0].calls if c[0] == "cancel"]  # cancelled, fail closed
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404


def test_l3_a_view_without_the_verbatim_risk_line_is_not_shown(client, token, stub, script):
    prepare(client, token, stub)
    script.view = {"view": {**script.view["view"], "risk": "Convenience signing is fine."}, "view_digest": HASH}
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_the_risk_constant_is_core_s(script):
    from orch.click.protocol import RISK

    from orch_dashboard import click

    assert click.RISK == RISK


def test_l4_a_slow_sign_does_not_block_the_workspace_list(client, token, stub, script):
    prepare(client, token, stub)
    assert client.get("/api/workspaces", headers=hdr(token, False)).json()[0]["click"]  # fills the last-known answer
    gate, entered = threading.Event(), threading.Event()

    def slow(op, args):
        if op == "sign":
            entered.set()
            gate.wait(10)
        return script.handler(op, args)

    script.clients[0].handler = slow
    out = []
    t = threading.Thread(
        target=lambda: out.append(
            client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
        )
    )
    t.start()
    assert entered.wait(5)
    t0 = time.monotonic()
    listing = client.get("/api/workspaces", headers=hdr(token, False))
    card = client.get(url(stub, "status"), headers=hdr(token, False))
    assert listing.status_code == 200 and card.status_code == 200 and time.monotonic() - t0 < 3
    assert listing.json()[0]["click"]["custody"] == "convenience"  # the last known answer
    gate.set()
    t.join(10)
    assert out and out[0].status_code == 200


def test_l5_after_a_restart_the_orphaned_requests_are_cancelled_and_free_their_slots(
    client, token, stub, script, gateway, host_check
):
    clock = [100.0]
    gateway.click.clock = lambda: clock[0]
    for n in range(8):
        assert client.post(url(stub, "prepare"), json=comment(4000 + n), headers=hdr(token)).status_code == 200
    assert client.post(url(stub, "prepare"), json=comment(4100), headers=hdr(token)).status_code == 503  # the quota
    first = script.clients[0]

    class Dead:
        def poll(self):
            return 1

    first.proc = Dead()
    clock[0] += 5
    script.refuse["view"] = ClickError("human.click_refused", "unknown", reason="not_prepared_here")
    res = client.post(url(stub, "view"), json={"request_id": f"hr_{1:026d}"}, headers=hdr(token))
    assert res.json()["error"]["reason"] == "not_prepared_here"
    assert sorted(host_check.cancelled) == [f"hr_{n:026d}" for n in range(1, 9)]  # cancelled at the host, by us
    script.refuse.clear()
    assert client.post(url(stub, "prepare"), json=comment(4101), headers=hdr(token)).status_code == 200  # slots free


def test_l5_cancel_of_a_request_the_signer_forgot_still_frees_it(client, token, stub, script, host_check):
    prepare(client, token, stub)
    script.refuse["cancel"] = ClickError("human.click_refused", "unknown", reason="not_prepared_here")
    res = client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409
    assert REQ in host_check.cancelled  # cancelled through our own link
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404


def test_n10_only_the_agent_label_is_shown_as_such():
    from orch_dashboard.adapters.tickets import _attested

    assert [_attested(v) for v in ("agent", "person", "verified", "host", "a" * 40, 7, None, "")] == ["agent"] + [
        "unknown"
    ] * 7


# -- Opus re-check (N1-N11): while a signature's outcome is unknown, nothing leads to signing the same action again ----


def lose_sign(script):
    def timeout(op, args):
        if op == "sign":
            raise TimeoutError
        return script.handler(op, args)

    script.clients[0].handler = timeout
    script.clients[0].proc = Alive()


def sign_lost(client, token, stub, rid=REQ):
    return client.post(url(stub, "sign"), json={"request_id": rid, "view_digest": HASH}, headers=hdr(token))


def other_session(client, auth):
    return client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()[
        "token"
    ]


def test_n4_an_unsettled_action_cannot_be_prepared_again_by_click_or_handoff_and_names_the_request(
    client, token, stub, script, auth, rich
):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    again = comment(2)  # the same action, a new click (new key)
    for who in (token, other_session(client, auth)):
        res = client.post(url(stub, "prepare"), json=again, headers=hdr(who))
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "click.unsettled" and res.json()["error"]["request_id"] == REQ
        hand = client.post(f"/api/w/{stub.ws}/handoff", json=again, headers=hdr(who))
        assert hand.json()["error"]["code"] == "click.unsettled" and hand.json()["error"]["request_id"] == REQ
    # Another action is not blocked.
    assert (
        client.post(url(stub, "prepare"), json={**comment(3), "message": "other"}, headers=hdr(token)).status_code
        == 200
    )


def test_n4_the_other_session_follows_it_and_the_host_settling_lifts_the_block(
    client, token, stub, script, auth, host_check, rich
):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    sign_lost(client, token, stub)
    second = other_session(client, auth)
    host_check.pending[REQ] = "bound"
    got = client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second))
    assert got.status_code == 200 and got.json()["state"] == "bound"  # re-attached: not `not_found`
    assert client.post(url(stub, "prepare"), json=comment(9), headers=hdr(second)).status_code == 409
    host_check.pending[REQ] = "expired"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second)).json()["state"] == "expired"
    assert client.post(url(stub, "prepare"), json=comment(10), headers=hdr(second)).status_code == 200


def test_n4_logout_does_not_cancel_an_unsettled_request(client, token, stub, script, host_check, rich):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    sign_lost(client, token, stub)
    client.post("/auth/logout", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    assert [c for c in script.clients[0].calls if c[0] == "cancel"] == [] and host_check.cancelled == []


def test_n4_cancel_of_an_unsettled_request_is_refused(client, token, stub, script, rich):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    sign_lost(client, token, stub)
    res = client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"
    assert [c for c in script.clients[0].calls if c[0] == "cancel"] == []


def test_n1_a_lost_channel_advances_the_epoch_and_the_sweep_spares_the_unsettled(
    client, token, stub, script, gateway, host_check, rich
):
    clock = [100.0]
    gateway.click.clock = lambda: clock[0]
    prepare(client, token, stub, comment(1))  # REQ: its sign will be lost
    prepare(client, token, stub, {**comment(2), "message": "second"})  # an ordinary orphan
    first = script.clients[0]

    def eof(op, args):
        if op == "sign":
            raise BrokenPipeError
        return script.handler(op, args)

    first.handler = eof
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    assert gateway.click.restarts == 1  # the drop itself advanced the epoch (it used to stay 0)
    clock[0] += 5
    assert (
        client.post(url(stub, "prepare"), json={**comment(5), "message": "third"}, headers=hdr(token)).status_code
        == 200
    )
    assert len(script.started) == 2  # restarted, and the prepare swept
    assert host_check.cancelled == [f"hr_{2:026d}"]  # the ordinary orphan, cancelled at the host
    host_check.pending[REQ] = "bound"
    assert (
        client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "bound"
    )  # kept


def test_n8_an_unknown_or_missing_host_state_is_unknown_not_a_500(client, token, stub, script, host_check, rich):
    prepare(client, token, stub, comment(1))
    for odd in ("weird", None):
        host_check.pending[REQ] = odd
        res = client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token))
        assert res.status_code == 200 and res.json()["state"] == "unknown"


def test_n7_state_bodies_are_small(client, token, stub, script):
    res = client.post(url(stub, "state"), json={"request_id": REQ, "x": "y" * 20000}, headers=hdr(token))
    assert res.status_code == 413


def test_n6_status_never_waits_for_the_channel(script, stub, tmp_path):
    from orch_dashboard.click_signer import Signer

    signer = Signer(tmp_path, start=script.start)
    assert signer.status()  # fills the last answer
    held = threading.Event()

    def hold():
        with signer.chan:
            held.set()
            time.sleep(1.0)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(2)
    t0 = time.monotonic()
    assert signer.status() == signer._last
    assert time.monotonic() - t0 < 0.3
    t.join()


def test_n11_a_hung_signer_is_replaced_only_when_nothing_is_unsettled(script, stub, tmp_path):
    from orch_dashboard.click_signer import Signer
    from orch_dashboard.errors import HostError

    clock = [100.0]
    signer = Signer(tmp_path, start=script.start, clock=lambda: clock[0])
    signer.call("status", {})
    first = script.clients[0]
    first.proc = Alive()
    first.handler = lambda op, args: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(HostError) as err:
        signer.call("prepare", {})
    assert "did not answer" in err.value.error["message"] and not first.closed and signer.suspect
    signer.unsettled = lambda: True  # a signature may be mid-submit: the hung signer stays
    clock[0] += 5
    with pytest.raises(HostError):
        signer.call("view", {})
    assert not first.closed and len(script.started) == 1
    signer.unsettled = lambda: False
    clock[0] += 5
    signer.call("status", {})
    assert first.closed and len(script.started) == 2 and signer.restarts == 1


def test_the_unsettled_error_status_is_a_409_with_the_request_id_only(client, token, stub, script, rich):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    sign_lost(client, token, stub)
    err = client.post(url(stub, "prepare"), json=comment(2), headers=hdr(token)).json()["error"]
    assert set(err) == {"code", "message", "retryable", "request_id"}
