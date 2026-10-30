"""H5c click-to-sign, the server side: the signer child's lifecycle, the four typed routes, error mapping and the
rules the section 0.8.6 item 7 puts on this process. The signer is faked here (a scripted channel); the real signer
runs in test_realhost_click.py."""

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from datetime import UTC
from pathlib import Path

import pytest
from conftest import ORIGIN
from orch.click.protocol import HUB_SELECTIVE_RISK, ClickError

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
        self.ws, self.enabled, self.available, self.contract = ws, True, True, 4
        self.features: list | None = None  # `status.features`: ["custody"] for a signer with the switch
        self.refuse: dict[str, ClickError] = {}
        self.offer: dict = {}  # the answer to `custody_off {begin}`
        self.rotation: dict = {}  # the answer to `rotate {begin}`
        self.github: dict = {}  # the answer to `github_token {begin}` (#1198 C16)
        self.runner_on = False  # dashboard execution, as the fake signer holds it (#1198 C14)
        self.view = {
            "view": {
                "risk": RISK,
                "lines": [CANARY],
                "op": "approve",
                "headline": "Approve DEMO-0001",
                "risk_lines": [],
            },
            "view_digest": HASH,
        }
        self.prepared = 0
        self.intents: dict[str, dict] = {}  # the host's idem record: idem -> {binding, request id, state}
        self.states: dict[str, str] = {}  # request id -> state at the (faked) host
        self.delay = 0.0
        self.started: list[dict] = []
        self.clients: list[Fake] = []
        self.nonces: list[bytes] = []  # the step-up nonces it issued

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
            out = {"core": "2.0.0.dev0", "contract": self.contract, "workspaces": [row]}
            if self.features is not None:
                out["features"] = self.features
            return out
        if op == "cancel_holds":
            return {"state": "cancelled"}
        if op == "stash":
            return {"state": "complete"}
        if op == "stepup":  # #1198 R1-6: the signer's one-use nonce for a passkey step-up
            self.nonces.append(secrets.token_bytes(32))
            return {"nonce": base64.urlsafe_b64encode(self.nonces[-1]).rstrip(b"=").decode()}
        if op == "runner":
            on = args["on"]
            if args.get("review"):
                lines = [f"Turn {'on' if on else 'off'} dashboard execution for demo."]
                lines += ["Agents may run commands on this computer as you when you start them."] if on else []
                view = {"op": f"runner.{'on' if on else 'off'}", "workspace": {"id": self.ws}, "on": on,
                        "terminals": bool(on and args.get("terminals")),
                        "confirm": {"kind": "click"}, "lines": lines}  # fmt: skip
                return {"view": view, "view_digest": HASH}
            self.runner_on = on
            return {"workspace_id": self.ws, "enabled": on, "terminals": on and args["terminals"]}
        if op == "github_token":
            if args.get("begin"):
                return self.github
            return {"workspace_id": self.ws, "credential": "missing" if args.get("remove") else "stored"}
        if op == "rotate":
            if args.get("begin"):
                return self.rotation
            return {"workspace_id": self.ws, "custody": "passphrase", "enabled": False, "done": True}
        if op == "custody_off" and args.get("begin"):
            return self.offer
        if op in ("custody_on", "custody_off"):
            self.enabled = self.available = op == "custody_on"
            row = {"workspace_id": self.ws, "custody": "convenience", "enabled": self.enabled}
            return {**row, "available": self.available, **({} if self.available else {"problem": "disabled"})}
        if op == "prepare":
            time.sleep(self.delay)
            binding = repr((args["op"], args["args"], args.get("files")))
            known = self.intents.get(args["idem"])
            if known is not None:  # the host honours opts.idem, and the signer forwards it
                if known["binding"] != binding:
                    raise ClickError("idem.conflict", "this idem was used for other arguments")
                if self.states[known["rid"]] != "bound":
                    raise ClickError("human.request_state", f"the request is {self.states[known['rid']]}")
                return {
                    "request_id": known["rid"],
                    "expires_at": "2026-10-12T12:10:00Z",
                    "op": args["op"],
                    "confirm": "click",
                }
            self.prepared += 1
            rid = f"hr_{self.prepared:026d}"
            self.intents[args["idem"]] = {"binding": binding, "rid": rid}
            self.states[rid] = "bound"
            return {
                "request_id": rid,
                "expires_at": "2026-10-12T12:10:00Z",
                "op": args["op"],
                "confirm": "click",
            }
        if op == "view":
            return self.view
        if op == "sign":
            if self.states.get(args["request_id"]) != "bound":  # the signer signs only a bound request
                raise ClickError("human.request_state", f"the request is {self.states.get(args['request_id'])}")
            self.states[args["request_id"]] = "submitted"
            return {"result": {"data": {"secret": "no"}, "key": "DEMO-0001", "seq": 7}, "duplicate": False}
        if op == "cancel":
            self.states[args["request_id"]] = "cancelled"
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
        self.listing: list[dict] = []  # what `human.pending` without arguments lists (terminal hand-offs have a code)
        self.review: list | None = None  # the committed events the host's record answers
        self.replay_error: str | None = None  # an idem code the host answers to the "Already done" replay
        self.verified: dict = {}  # what `adapters.handoff.request` answers (the verified record)
        self.key: str | None = None  # the ticket key of the host's row, once a sequence created it

    def human(self, ws, op, args, **kw):
        if op == "human.prepare" and self.replay_error:
            from orch_dashboard.errors import fail

            raise fail(self.replay_error, exit=2)
        if op in ("human.get", "human.prepare"):
            self.reads.append(args)
            return {
                "op": "log",
                "request": {"id": REQ, "state": "submitted", "submitted_at": "2026-10-13T15:20:00Z"},
                "material": {"review": self.review or [{"type": "log.added", "payload": {"message": "host text"}}]},
            }
        if op == "human.cancel":
            self.cancelled.append(args["request_id"])
            return {"state": "cancelled"}
        if op == "human.pending":
            if "request_id" not in args:
                return {"requests": list(self.listing)}
            return {
                "requests": [
                    {
                        "id": args["request_id"],
                        "state": self.pending.get(args["request_id"], "bound"),
                        "seq": 7,
                        **({"key": self.key, "created_at": "2026-10-19T08:00:00Z"} if self.key else {}),
                    }
                ]
            }
        raise AssertionError(op)

    def request(self, prepared, ws, **kw):
        self.checks.append(kw)
        if self.mismatch:
            from orch_dashboard.errors import fail

            raise fail("internal", "x")
        return self.verified


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
    return {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}


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
    assert got[0]["click"] == {"custody": "convenience", "enabled": True, "available": True, "risk_ack": None}


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


@pytest.mark.parametrize("contract", [1, 2, 3, 5, 0, True, "4", None, 4.0])
def test_a_signer_of_another_contract_is_refused(client, token, stub, script, contract):
    script.contract = contract  # strict: only the int 4 (4.0 == 4 in Python must not pass)
    ws = client.get("/api/workspaces", headers=hdr(token, False)).json()[0]
    assert "click" not in ws["features"] and ws["click"] is None
    assert script.clients[0].closed  # that child is stopped
    res = prepare(client, token, stub)
    assert res.status_code == 501 and res.json()["error"]["code"] == "click.contract"
    assert "orch-core" in res.json()["error"]["message"]
    assert [c for c in script.clients[0].calls if c[0] == "prepare"] == []


def test_a_contract_4_signer_is_accepted_and_remembered(client, token, stub, script, gateway):
    script.contract = 4
    ws = client.get("/api/workspaces", headers=hdr(token, False)).json()[0]
    assert "click" in ws["features"] and ws["click"]["available"] is True
    res = prepare(client, token, stub)
    assert res.status_code == 200, res.text
    assert gateway.click.contract == 4


def test_prepare_chooses_the_op_here_and_derives_the_idem_from_the_action(client, token, stub, script, auth):
    res = prepare(client, token, stub)
    assert res.status_code == 200, res.text
    assert res.json() == {"request_id": REQ, "expires_at": "2026-10-12T12:10:00Z", "op": "approve", "confirm": "click"}
    op, args = [c for c in script.clients[0].calls if c[0] == "prepare"][0]
    assert args["workspace_id"] == stub.ws and args["op"] == "approve"
    assert args["args"] == {"ref": "DEMO-0001", "gate": "requirements"}
    assert re.fullmatch(r"a_[0-7][0-9A-HJKMNP-TV-Z]{25}", args["idem"])
    # The same action again, from this tab or another session: the SAME key, so the host returns the SAME request.
    other = client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()
    for who in (token, other["token"]):
        again = prepare(client, who, stub)
        assert again.status_code == 200 and again.json()["request_id"] == REQ
    keys = {c[1]["idem"] for c in script.clients[0].calls if c[0] == "prepare"}
    assert keys == {args["idem"]} and script.prepared == 1
    # An explicit "do it again" is another intent.
    more = prepare(client, token, stub, {**approve(), "retry": 1})
    assert more.json()["request_id"] != REQ and script.prepared == 2


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
        res = prepare(client, token, stub, {"action": action})
        assert res.status_code == 400
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_a_member_add_sends_the_certificate_as_a_file(client, token, stub, script):
    import base64
    import json

    pk = base64.urlsafe_b64encode(b"\x04" + b"a" * 64).rstrip(b"=").decode()
    cert = json.dumps({"v": 1})
    body = {"action": "member_add", "name": "Mara", "role": "member", "pk": pk, "cert": cert}
    res = prepare(client, token, stub, body)
    assert res.status_code == 200, res.text
    args = [c for c in script.clients[0].calls if c[0] == "prepare"][0][1]
    assert args["op"] == "member.add" and args["args"]["cert"] == "-"
    assert base64.b64decode(args["files"]["cert"]["b64"]) == cert.encode()
    assert "reader_decl" not in args["args"] and set(args["files"]) == {"cert"}


def test_a_member_add_sends_the_reader_declaration_as_its_own_file(client, token, stub, script):
    import base64
    import json

    pk = base64.urlsafe_b64encode(b"\x04" + b"a" * 64).rstrip(b"=").decode()
    cert, decl = json.dumps({"v": 1}), json.dumps({"o": {"kind": "reader_decl"}, "sig": "x"})
    body = {"action": "member_add", "name": "Mara", "role": "member", "pk": pk, "cert": cert, "reader_decl": decl}
    res = prepare(client, token, stub, body)
    assert res.status_code == 200, res.text
    args = [c for c in script.clients[0].calls if c[0] == "prepare"][0][1]
    assert args["args"]["cert"] == "-" and args["args"]["reader_decl"] not in ("-", None)
    assert base64.b64decode(args["files"]["reader_decl"]["b64"]) == decl.encode()
    assert base64.b64decode(args["files"]["cert"]["b64"]) == cert.encode()


@pytest.mark.parametrize("decl", ["[]", "{", '{"a": 1.5}', '{"a": 1, "a": 2}', '{"o": "' + "x" * 4096 + '"}'])
def test_a_member_add_with_a_reader_declaration_that_is_no_strict_json_object_is_refused(
    client, token, stub, script, decl
):
    import base64
    import json

    pk = base64.urlsafe_b64encode(b"\x04" + b"a" * 64).rstrip(b"=").decode()
    body = {"action": "member_add", "name": "Mara", "role": "member", "pk": pk, "cert": json.dumps({"v": 1})}
    res = prepare(client, token, stub, {**body, "reader_decl": decl})
    assert res.status_code == 400, res.text
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_view_is_the_signers_view_unchanged(client, token, stub, script):
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200
    assert res.json() == script.view  # nothing added, hidden or re-rendered
    assert res.headers["cache-control"] == "no-store"
    call = [c for c in script.clients[0].calls if c[0] == "view"][0]
    assert call[1] == {"workspace_id": stub.ws, "request_id": REQ, "contract": 4}  # run 13 C3: announced


def test_sign_sends_exactly_the_digest_and_answers_without_the_hosts_result(client, token, stub, script):
    prepare(client, token, stub)
    body = {"request_id": REQ, "view_digest": HASH}
    res = client.post(url(stub, "sign"), json=body, headers=hdr(token))
    assert res.status_code == 200
    assert res.json() == {"state": "submitted", "duplicate": False, "key": "DEMO-0001", "seq": 7}
    sent = [c for c in script.clients[0].calls if c[0] == "sign"][0][1]
    assert sent == {
        "workspace_id": stub.ws,
        "request_id": REQ,
        "view_digest": HASH,
        "contract": 4,
        "delay_s": 0,
        "session": hashlib.sha256(token.encode()).hexdigest(),
    }


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": REQ, "view_digest": "sha256:short"},
        {"request_id": REQ, "view_digest": HASH, "word": "DEMO~ab"},  # no word field any more
        {"request_id": "not-a-request", "view_digest": HASH},
        {"request_id": REQ, "view_digest": HASH, "extra": 1},
    ],
)
def test_sign_bodies_are_checked(client, token, stub, script, body):
    prepare(client, token, stub)
    assert client.post(url(stub, "sign"), json=body, headers=hdr(token)).status_code == 400
    assert [c for c in script.clients[0].calls if c[0] == "sign"] == []


def test_a_request_belongs_only_to_the_preparing_session(client, token, stub, script, auth):
    prepare(client, token, stub)
    other = client.post("/auth/session", json={"code": auth.launch(43210)["code"]}, headers={"Origin": ORIGIN}).json()
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(other["token"]))
    assert res.status_code == 200
    unknown = "hr_" + "0" * 25 + "2"
    for step, body in (
        ("view", {"request_id": unknown}),
        ("sign", {"request_id": unknown, "view_digest": HASH}),
        ("cancel", {"request_id": unknown}),
    ):
        # an id nobody here prepared looks like one that never existed, whoever asks
        for who in (token, other["token"]):
            assert client.post(url(stub, step), json=body, headers=hdr(who)).status_code == 404, step


def test_a_different_person_never_reaches_the_request(client, token, stub, script, monkeypatch):
    prepare(client, token, stub)
    monkeypatch.setattr(client.app.state, "person_of", lambda ws: "p_" + "f" * 32)
    for step, body in (
        ("view", {"request_id": REQ}),
        ("sign", {"request_id": REQ, "view_digest": HASH}),
        ("cancel", {"request_id": REQ}),
        ("state", {"request_id": REQ}),
    ):
        assert client.post(url(stub, step), json=body, headers=hdr(token)).status_code == 404, step
    assert [c for c in script.clients[0].calls if c[0] in ("view", "sign", "cancel")] == []


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
        (ClickError("sign.convenience_unavailable", "other_member: x", reason="other_member"), 409),
        (ClickError("sign.convenience_readers", "update the readers: relay"), 409),
        (ClickError("rate.limited", "slow down", retry_after_ms=4200, retryable=True), 503),
        (ClickError("lock.busy", "busy, try again shortly", retryable=True), 503),
        (ClickError("custody.no_key", "no key"), 409),
        (ClickError("custody.unavailable", "locked"), 409),
        (ClickError("host.version_mismatch", "differs"), 501),
        (ClickError("host.unavailable", "gone"), 503),
        (ClickError("human.request_expired", "late"), 410),
        (ClickError("human.request_state", "submitted"), 409),
        (ClickError("human.click_refused", "short", reason="too_soon"), 409),
        (ClickError("human.click_refused", "short", reason="view_changed"), 409),
        (ClickError("human.click_refused", "no", reason="terminal_only"), 409),
        (ClickError("human.click_refused", "signed", reason="already_signed"), 409),
        (ClickError("human.click_refused", "corrupt", reason="record_unavailable"), 409),
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
    if error.code == "lock.busy":
        assert res.json()["exit"] == 9 and got["retryable"] is True
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
    client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    out = capfd.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert CANARY not in text and "TYPED-WORD" not in text and token not in text


def test_the_sign_routes_take_small_bodies(client, token, stub, script):
    prepare(client, token, stub)
    big = {"request_id": REQ, "view_digest": HASH, "pad": "x" * 20000}
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
    # the gateway reads `status` for the card; the step-up ceremony asks for a passkey nonce (`stepup`); nothing else
    assert holders == ["click.py", "gateway.py", "stepup_routes.py"]
    assert [m for m in re.findall(r'signer\.call\("(\w+)"', (root / "stepup_routes.py").read_text())] == ["stepup"]
    assert "orch.click" not in (root / "signing.py").read_text()


def test_the_status_route_is_the_card_only(client, token, stub, script):
    res = client.get(url(stub, "status"), headers=hdr(token, False))
    assert res.json() == {
        "custody": "convenience",
        "enabled": True,
        "available": True,
        "risk_ack": None,
        "switchable": False,
    }
    script.available = False
    assert client.get(url(stub, "status"), headers=hdr(token, False)).json()["problem"] == "disabled"


# -- parity with the hand-off: every gate of its routes, on the click routes too -------------------------------------


def comment(n):
    return {"action": "comment", "key": "DEMO-0001", "message": f"hello {n}"}


def policy_body(n):
    policy = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
    return {"action": "policy_set", "gate": "requirements", "policy": policy}


def families(stub):
    """(name, prepare url, request-scoped calls: (method, url-for-an-id) per step)."""
    click = f"/api/w/{stub.ws}/click"
    return {
        "click": {
            "prepare": f"{click}/prepare",
            "steps": [("post", lambda r: f"{click}/view"), ("post", lambda r: f"{click}/cancel")],
        },
    }


@pytest.fixture(params=["click"])
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
    assert mutate(client, "post", f["prepare"], token, {"action": "grant"}).status_code == 400
    assert mutate(client, "post", f["prepare"], token, {**ok, "idem": "a_" + "0" * 26}).status_code == 400  # no key
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


def test_parity_other_people_and_unknown_requests_look_like_none(client, token, stub, script, family, monkeypatch):
    name, f = family
    made = mutate(client, "post", f["prepare"], token, approve(904))
    assert made.status_code == 200, made.text
    rid = made.json().get("id") or made.json()["request_id"]
    unknown = "hr_" + "0" * 24 + "77"
    monkeypatch.setattr(client.app.state, "person_of", lambda ws: "p_" + "f" * 32)  # not the person who prepared it
    for method, build in f["steps"]:
        for known in (rid, unknown):
            body = {"request_id": known} if method == "post" else None
            res = (
                mutate(client, method, build(known), token, body)
                if method != "get"
                else send(client, "get", build(known), token)
            )
            assert res.status_code == 404, (name, method, res.text)


def test_parity_the_per_session_quota(client, token, stub, script, family):
    name, f = family
    for n in range(8):
        res = mutate(client, "post", f["prepare"], token, comment(910 + n))
        assert res.status_code == 200, (name, n, res.text)
    ninth = mutate(client, "post", f["prepare"], token, comment(930))
    assert ninth.status_code == 503 and code(ninth) == "quota.host", (name, ninth.text)


def test_parity_the_same_action_is_the_same_request_and_another_is_another(client, token, stub, script, family):
    name, f = family
    first = mutate(client, "post", f["prepare"], token, comment(940))
    again = mutate(client, "post", f["prepare"], token, comment(940))  # the same click again, or another tab
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    other = mutate(client, "post", f["prepare"], token, comment(941))
    rid = lambda r: r.json().get("id") or r.json()["request_id"]  # noqa: E731
    assert other.status_code == 200 and rid(other) != rid(first)


def test_a_view_for_another_op_than_the_one_asked_for_is_refused(client, token, stub, script):
    prepare(client, token, stub)
    script.view = {"view": {**script.view["view"], "op": "close"}, "view_digest": HASH}
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_concurrent_prepares_cannot_overshoot_the_one_quota(client, token, stub, script, rich):
    """Check and reserve of the session's quota are one step."""
    script.delay = 0.05  # a wide window between "checked" and "committed"
    results = []

    def go(n):
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
    from orch_dashboard.signing import SettingsSet

    body = {"action": "settings_set", "grant_hours": 12}
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


@pytest.mark.parametrize(
    "bad",
    [
        {"headline": None},
        {"headline": ""},
        {"headline": "two\nlines"},
        {"headline": 3},
        {"risk_lines": None},
        {"risk_lines": "one line"},
        {"risk_lines": [3]},
        {"risk_lines": [""]},
        {"risk_lines": ["a\nb"]},
        {"risk_lines": ["x"] * 65},
        {"headline_detail": ""},
        {"headline_detail": "“two\nlines”"},
        {"headline_detail": 3},
    ],
)
def test_c3_a_view_without_its_headline_or_risk_lines_is_not_shown(client, token, stub, script, bad):
    """Contract 4 (run 13 C3): `headline` (one line) and `risk_lines` (lines, maybe none) are the signer's; a View that
    lacks them, or carries another shape, is not passed on."""
    prepare(client, token, stub)
    view = {**script.view["view"], **bad}
    script.view = {"view": {k: v for k, v in view.items() if v is not None}, "view_digest": HASH}
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"
    assert CANARY not in res.text


def test_c3_the_view_and_its_risk_lines_are_passed_on_unchanged(client, token, stub, script):
    prepare(client, token, stub)
    risk = []  # approve is on the closed one-press allowlist
    view = {**script.view["view"], "risk_lines": risk, "headline_detail": "“Export does nothing”"}
    script.view = {"view": view, "view_digest": HASH}
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == script.view  # nothing added, hidden or re-rendered


def test_c3_this_server_announces_its_contract_on_prepare_view_and_sign_only(client, token, stub, script):
    prepare(client, token, stub)
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 200
    signed = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    assert signed.status_code == 200, signed.text
    calls = script.clients[0].calls
    announced = {op for op, args in calls if "contract" in args}
    assert announced == {"prepare", "view", "sign"}
    assert all(args["contract"] == 4 and type(args["contract"]) is int for op, args in calls if op in announced)


def test_c3_a_signer_refusing_the_announced_contract_is_click_contract(client, token, stub, script):
    script.refuse["prepare"] = ClickError("contract.mismatch", "this signer speaks click contract 5")
    res = prepare(client, token, stub)
    assert res.status_code == 501 and res.json()["error"]["code"] == "click.contract"
    assert "orch-core" in res.json()["error"]["message"]


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


def test_n4_after_an_uncertain_sign_the_same_action_from_any_tab_is_the_same_request(
    client, token, stub, script, auth, rich
):
    """Nothing in this process blocks the repeat: the intent idem makes the host (here its faked record) answer with
    the request that already exists, so there is never a second request to sign."""
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    for who in (token, other_session(client, auth)):
        res = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(who))
        assert res.status_code == 200 and res.json()["request_id"] == REQ
    assert script.prepared == 1
    # Another action is another request.
    other = client.post(url(stub, "prepare"), json={**comment(3), "message": "other"}, headers=hdr(token))
    assert other.status_code == 200 and other.json()["request_id"] != REQ


def test_n4_the_other_session_follows_it_to_its_end(client, token, stub, script, auth, host_check, rich):
    prepare(client, token, stub, comment(1))
    lose_sign(script)
    sign_lost(client, token, stub)
    second = other_session(client, auth)
    host_check.pending[REQ] = "bound"
    got = client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second))
    assert got.status_code == 200 and got.json()["state"] == "bound"  # the person's request, not another tab's
    host_check.pending[REQ] = "expired"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second)).json()["state"] == "expired"
    assert not client.app.state.handoffs.any_unsettled()  # the host said so: nothing is unsettled any more


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
    assert time.monotonic() - t0 < 0.6  # waiting for the lock would take the 1 s hold; idle takes ~1 ms
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


# -- Opus re-check 2 (N12-N17): two tabs, in-flight signs, adoption, quota, recently signed ----------------------------


def test_n12_two_tabs_one_request_and_a_sign_in_flight_is_never_cancelled_or_replaced(
    client, token, stub, script, auth, rich, host_check
):
    prepare(client, token, stub, comment(1))
    go, entered = threading.Event(), threading.Event()

    def slow(op, args):
        if op == "sign":
            entered.set()
            go.wait(5)
            raise TimeoutError
        return script.handler(op, args)

    script.clients[0].handler = slow
    script.clients[0].proc = Alive()
    second = other_session(client, auth)
    out = {}
    ta = threading.Thread(target=lambda: out.__setitem__("a", sign_lost(client, token, stub)))
    ta.start()
    assert entered.wait(5)
    # The second tab prepares the same action during the sign: the same request (not a new one), and while the
    # signature is in flight neither tab can cancel it from under the signer.
    during = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(second))
    assert during.status_code == 200 and during.json()["request_id"] == REQ and script.prepared == 1
    refused = client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(second))
    assert refused.status_code == 409 and refused.json()["error"]["reason"] == "check_failed"
    go.set()
    ta.join(10)
    assert out["a"].json()["error"]["reason"] == "uncertain"
    assert [c for c in script.clients[0].calls if c[0] == "cancel"] == [] and host_check.cancelled == []


def test_n12_unsettled_requests_are_tracked_one_by_one(client, token, stub, script, auth, rich):
    handoffs = client.app.state.handoffs
    for rid in ("hr_a", "hr_b"):  # an unknown sign outcome is the only thing that leaves a mark behind
        handoffs.begin_sign(stub.ws, rid, handoffs.clock() + 100)
        handoffs.end_sign(stub.ws, rid, handoffs.clock() + 100, "uncertain")
    assert handoffs.is_unsettled("hr_a") == stub.ws and handoffs.is_unsettled("hr_b") == stub.ws
    handoffs.settled("hr_b")
    assert handoffs.is_unsettled("hr_a") == stub.ws and handoffs.is_unsettled("hr_b") is None


def test_n12_a_definite_refusal_clears_the_in_flight_mark_and_success_clears_it_too(client, token, stub, script, rich):
    prepare(client, token, stub, comment(1))
    handoffs = client.app.state.handoffs
    script.refuse["sign"] = ClickError("human.click_refused", "short", reason="too_soon")
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "too_soon"
    assert handoffs.is_unsettled(REQ) is None and not handoffs.any_unsettled()  # retry is allowed
    script.refuse.clear()
    assert prepare(client, token, stub, comment(1)).status_code == 200
    assert sign_lost(client, token, stub).status_code == 200
    assert not handoffs.any_unsettled()


def test_local_non_host_error_before_sign_clears_mark(client, token, stub, script, gateway, monkeypatch):
    prepare(client, token, stub, comment(1))
    original = gateway.click._ensure

    def local_failure():
        raise ValueError("local validation failed before using the channel")

    monkeypatch.setattr(gateway.click, "_ensure", local_failure)
    assert sign_lost(client, token, stub).json()["error"]["code"] == "click.unavailable"
    assert not client.app.state.handoffs.any_unsettled()
    assert not [call for call in script.clients[0].calls if call[0] == "sign"]
    monkeypatch.setattr(gateway.click, "_ensure", original)
    assert prepare(client, token, stub, comment(1)).status_code == 200
    assert sign_lost(client, token, stub).status_code == 200


def test_definite_refusal_clears_mark_even_when_follow_up_host_read_fails(client, token, stub, script, monkeypatch):
    from orch_dashboard.errors import fail

    prepare(client, token, stub, comment(1))
    script.refuse["sign"] = ClickError("human.request_state", "the request is cancelled")
    original = client.app.state.human

    def unavailable(*args, **kwargs):
        raise fail("host.unavailable")

    monkeypatch.setattr(client.app.state, "human", unavailable)
    assert sign_lost(client, token, stub).json()["error"]["code"] == "host.unavailable"
    assert not client.app.state.handoffs.any_unsettled()
    monkeypatch.setattr(client.app.state, "human", original)
    script.refuse.clear()
    assert prepare(client, token, stub, comment(1)).status_code == 200


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, RuntimeError])
def test_unknown_channel_outcome_keeps_unsettled_mark(client, token, stub, script, error):
    prepare(client, token, stub, comment(1))

    def failed_channel(op, args):
        raise error("answer lost or unknown")

    script.clients[0].handler = failed_channel
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    assert client.app.state.handoffs.is_unsettled(REQ) == stub.ws


def test_signer_internal_error_is_unknown_not_a_definite_refusal(client, token, stub, script):
    prepare(client, token, stub, comment(1))
    script.refuse["sign"] = ClickError("internal", "failed after signing or encoding the result")
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    assert client.app.state.handoffs.is_unsettled(REQ) == stub.ws


def test_n13_a_second_session_of_the_person_may_view_sign_and_poll_the_same_request(
    client, token, stub, script, auth, host_check, rich
):
    prepare(client, token, stub, comment(1))
    script.view = {"view": {**script.view["view"], "op": "log"}, "view_digest": HASH}
    second = other_session(client, auth)
    host_check.pending[REQ] = "bound"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second)).status_code == 200
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(second)).status_code == 200
    signed = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(second))
    assert signed.status_code == 200 and signed.json()["state"] == "submitted"
    # The first tab's sign of the very same request: one signature at the signer/host, never a second request.
    assert script.prepared == 1


def test_n14_another_session_settling_it_frees_the_preparing_sessions_slot(
    client, token, stub, script, auth, host_check, rich
):
    for n in range(8):
        assert (
            client.post(
                url(stub, "prepare"), json={**comment(7000 + n), "message": f"m{n}"}, headers=hdr(token)
            ).status_code
            == 200
        )
    assert (
        client.post(url(stub, "prepare"), json={**comment(7100), "message": "ninth"}, headers=hdr(token)).status_code
        == 503
    )
    lose_sign(script)
    sign_lost(client, token, stub)  # REQ = request 1, unsettled
    second = other_session(client, auth)
    host_check.pending[REQ] = "expired"
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(second)).json()["state"] == "expired"
    script.clients[0].handler = script.handler
    assert (
        client.post(url(stub, "prepare"), json={**comment(7101), "message": "tenth"}, headers=hdr(token)).status_code
        == 200
    )


def test_n15_after_a_landed_signature_the_same_action_is_already_done_and_only_an_explicit_click_repeats_it(
    client, token, stub, script, host_check, rich
):
    prepare(client, token, stub, comment(1))
    assert sign_lost(client, token, stub).status_code == 200
    again = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    err = again.json()["error"]
    assert again.status_code == 409 and err["code"] == "intent.done" and err["state"] == "submitted"
    assert script.prepared == 1  # no second request was made
    # "Do it again" is an explicit click and a new intent.
    repeat = client.post(url(stub, "prepare"), json={**comment(1), "retry": 1}, headers=hdr(token))
    assert repeat.status_code == 200 and repeat.json()["request_id"] != REQ and script.prepared == 2


@pytest.mark.parametrize("host_code, mapped", [("idem.conflict", "intent.blocked"), ("idem.expired", "intent.expired")])
def test_already_done_replay_maps_idem_codes_like_send_prepare(
    client, token, stub, script, host_check, rich, host_code, mapped
):
    """Opus re-check N3: an idem error from the "Already done" replay reaches the browser mapped, never as a raw host
    code, and with the warning that an earlier attempt may have been signed (N2)."""
    prepare(client, token, stub, comment(1))
    assert sign_lost(client, token, stub).status_code == 200
    host_check.replay_error = host_code
    again = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    err = again.json()["error"]
    assert again.status_code == 409 and err["code"] == mapped
    assert "may have been signed" in err["message"]
    assert script.prepared == 1  # still no second request


def test_n15_a_cancelled_or_expired_attempt_is_ended_and_pressing_again_is_a_fresh_request(
    client, token, stub, script, rich
):
    """#794: the server steps past ended attempts itself; it makes exactly one new request."""
    prepare(client, token, stub, comment(1))
    assert client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token)).status_code == 200
    fresh = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert fresh.status_code == 200 and fresh.json()["request_id"] != REQ and script.prepared == 2
    # cancel that one too (expired reads the same): the next press is another fresh one
    assert (
        client.post(
            url(stub, "cancel"), json={"request_id": fresh.json()["request_id"]}, headers=hdr(token)
        ).status_code
        == 200
    )
    third = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert third.status_code == 200 and script.prepared == 3
    # an open request is the same one for a second press (double click, second tab)
    twin = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert twin.json()["request_id"] == third.json()["request_id"] and script.prepared == 3


@pytest.mark.parametrize("ended", ["cancelled", "expired"])
def test_1087_an_ended_attempt_is_never_signed_later_and_pressing_again_is_a_fresh_request(
    client, token, stub, script, rich, ended
):
    """#794 removed `intent.ended`: the server steps past an attempt that ended unsigned. The ended request stays
    ended (a late sign of it is refused and changes nothing); only the fresh request can be signed, by its own sign."""
    prepare(client, token, stub, comment(1))
    if ended == "cancelled":
        assert client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token)).status_code == 200
    else:
        script.states[REQ] = "expired"  # the host let it expire
    late = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(token))
    assert late.status_code != 200 and script.states[REQ] == ended, late.text
    fresh = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert fresh.status_code == 200, fresh.text
    rid = fresh.json()["request_id"]
    assert rid != REQ and script.prepared == 2 and script.states[REQ] == ended
    assert script.states[rid] == "bound"  # prepared, not signed: signing it is the person's own next click


@pytest.mark.parametrize(
    "body",
    [
        {"action": "new_sequence", "title": "Plan the launch", "priority": "high", "sections": {"summary": "Why."}},
        {"action": "quick_start", "title": "Plan the launch", "description": "Why."},
    ],
)
def test_p06_a_new_ticket_with_the_text_of_an_earlier_one_shows_its_own_review(
    client, token, stub, script, host_check, rich, body
):
    """Run-10 J04c-01: an earlier ticket with the same title and text left an unreadable signed record. The first
    press must show a fresh review (a new request, its own idem), never the dead end;
    only the person's Sign signs it."""
    script.features = ["custody", "sequence"]
    first = prepare(client, token, stub, body)
    assert first.status_code == 200, first.text
    script.states[first.json()["request_id"]] = "submitted"  # the earlier ticket was signed
    host_check.replay_error = "idem.conflict"  # and its record cannot be read here
    again = prepare(client, token, stub, body)
    assert again.status_code == 200, again.text
    rid = again.json()["request_id"]
    assert rid != first.json()["request_id"] and script.prepared == 2
    assert script.states[rid] == "bound"  # prepared, not signed
    assert again.json()["repeat"] is True  # the page says: this makes another ticket like one already signed
    idems = [a["idem"] for c, a in script.clients[0].calls if c == "prepare"]
    assert len(set(idems)) == 2  # a new intent id: no earlier signature is reused


def test_p06_a_lost_response_repress_of_a_new_ticket_is_already_done_with_its_sequence_replayed(
    client, token, stub, script, host_check, rich
):
    script.features = ["custody", "sequence"]
    body = {"action": "new_sequence", "title": "Plan the launch", "priority": "high", "sections": {"summary": "Why."}}
    first = prepare(client, token, stub, body)
    script.states[first.json()["request_id"]] = "submitted"
    again = prepare(client, token, stub, body)
    assert again.status_code == 409 and again.json()["error"]["code"] == "intent.done"
    assert script.prepared == 1 and "sequence" not in again.json()  # no second ticket
    replay = [a for a in host_check.reads if "sequence" in a]
    assert replay and replay[0]["sequence"]["intent"]  # the host binds the sequence: it is sent along


def test_p06_when_the_stepped_retry_is_also_signed_it_steps_once_more(client, token, stub, script, host_check, rich):
    script.features = ["custody", "sequence"]
    body = {"action": "new_sequence", "title": "Plan the launch", "priority": "high", "sections": {"summary": "Why."}}
    a = prepare(client, token, stub, body).json()["request_id"]
    script.states[a] = "submitted"
    host_check.replay_error = "idem.conflict"
    b = prepare(client, token, stub, body).json()["request_id"]
    script.states[b] = "submitted"
    c = prepare(client, token, stub, body)
    assert c.status_code == 200, c.text
    assert c.json()["request_id"] not in (a, b) and c.json()["repeat"] is True and script.prepared == 3


def test_p06_already_done_names_the_created_ticket_in_plain_fields(client, token, stub, script, host_check, rich):
    script.features = ["custody", "sequence"]
    body = {"action": "new_sequence", "title": "Plan the launch", "priority": "high", "sections": {"summary": "Why."}}
    first = prepare(client, token, stub, body)
    script.states[first.json()["request_id"]] = "submitted"
    host_check.review = [
        {"type": "ticket.created", "key": "DEMO-0007", "title": "Plan the launch", "ticket_type": "feature"},
        {"type": "log.added", "payload": {"message": "x"}},
    ]
    err = prepare(client, token, stub, body).json()["error"]
    assert err["code"] == "intent.done" and err["ticket"] == {"key": "DEMO-0007", "title": "Plan the launch"}


def test_p06_a_comment_with_an_unreadable_signed_record_still_blocks(client, token, stub, script, host_check, rich):
    prepare(client, token, stub, comment(1))
    assert sign_lost(client, token, stub).status_code == 200
    host_check.replay_error = "idem.conflict"
    again = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert again.status_code == 409 and again.json()["error"]["code"] == "intent.blocked"
    assert script.prepared == 1


def test_794_a_signed_attempt_is_already_done_even_after_earlier_cancels(client, token, stub, script, rich):
    prepare(client, token, stub, comment(1))
    assert client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(token)).status_code == 200
    second = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token)).json()["request_id"]
    script.states[second] = "submitted"  # signed
    again = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert again.status_code == 409 and again.json()["error"]["code"] in (
        "intent.done",
        "intent.expired",
        "intent.blocked",
    )
    assert script.prepared == 2  # no third request


def test_794_the_walk_is_capped(client, token, stub, script, rich):
    from orch_dashboard.signing import NEXT_RETRY_HOPS

    for r in range(NEXT_RETRY_HOPS + 1):
        got = client.post(url(stub, "prepare"), json={**comment(1), "retry": r}, headers=hdr(token))
        assert got.status_code == 200, got.text
        client.post(url(stub, "cancel"), json={"request_id": got.json()["request_id"]}, headers=hdr(token))
    made = script.prepared
    res = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["code"] == "intent.exhausted"
    assert script.prepared == made
    ok = client.post(url(stub, "prepare"), json={**comment(1), "retry": NEXT_RETRY_HOPS + 1}, headers=hdr(token))
    assert ok.status_code == 200 and script.prepared == made + 1


def test_n15_an_idem_conflict_from_the_signer_is_an_open_attempt_not_a_second_request(
    client, token, stub, script, rich
):
    script.refuse["prepare"] = ClickError("idem.conflict", "bound to another session")
    res = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token))
    err = res.json()["error"]
    assert res.status_code == 409 and err["code"] == "intent.blocked" and "no request is open" in err["message"]
    assert len([c for c in script.clients[0].calls if c[0] == "prepare"]) == 1  # not retried under another key
    assert script.prepared == 0


def test_a_repeated_sign_with_the_same_view_is_the_recorded_result_not_a_new_signature(
    client, token, stub, script, rich
):
    prepare(client, token, stub, comment(1))
    first = sign_lost(client, token, stub)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    orig = script.handler

    def recorded(op, args):
        if op == "sign":
            return {"result": {"key": "DEMO-0001", "seq": 7}, "duplicate": True}
        return orig(op, args)

    script.clients[0].handler = recorded
    again = sign_lost(client, token, stub)
    assert again.status_code == 200 and again.json() == {
        "state": "submitted",
        "duplicate": True,
        "key": "DEMO-0001",
        "seq": 7,
    }


def test_already_signed_and_an_unreadable_record_never_cancel_or_forget_the_request(
    client, token, stub, script, host_check, rich
):
    prepare(client, token, stub, comment(1))
    for reason in ("already_signed", "record_unavailable"):
        script.refuse["sign"] = ClickError("human.click_refused", "x", reason=reason)
        res = sign_lost(client, token, stub)
        assert res.status_code == 409 and res.json()["error"]["reason"] == reason
    assert host_check.cancelled == [] and [c for c in script.clients[0].calls if c[0] == "cancel"] == []
    script.refuse.clear()
    host_check.pending[REQ] = "submitted"  # re-attach to its state
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "submitted"


# -- no unbound request is prepared any more: click/prepare is the only prepare route ---------------------------------


def test_no_unbound_request_can_be_prepared(client, token, stub, script, host_check, rich):
    for method in ("post", "get", "delete"):
        path = f"/api/w/{stub.ws}/handoff" + ("" if method == "post" else "/" + REQ)
        res = client.request(method, path, json=comment(1) if method == "post" else None, headers=hdr(token))
        assert res.status_code in (404, 405), (method, res.text)
    assert host_check.reads == []  # nothing reached the host


def test_an_open_unbound_request_of_the_same_intent_is_never_reused(client, token, stub, script, host_check):
    """A request prepared by the old terminal hand-off (no signer) has another host binding: the click prepare is
    refused (`intent.open`), never handed that request to sign."""
    script.refuse["prepare"] = ClickError("idem.conflict", "this idem was used for other arguments")
    host_check.listing = [
        {"id": REQ, "op": "log", "key": "DEMO-0001", "state": "prepared", "expires_at": "2026-10-13T15:30:00Z"}
    ]
    res = prepare(client, token, stub, comment(1))
    assert res.json()["error"]["code"] == "intent.open"
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404


def test_the_flag_flipped_off_ends_the_open_click_request(client, token, stub, script, host_check, rich):
    prepare(client, token, stub, comment(1))
    script.refuse["sign"] = ClickError("sign.convenience_unavailable", "disabled: off", reason="disabled")
    res = sign_lost(client, token, stub)
    assert res.json()["error"]["reason"] == "disabled"
    assert client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token)).status_code == 404  # ended


def test_submitted_sign_refusal_uses_host_record_warning(client, token, stub, script, host_check):
    prepare(client, token, stub, comment(1))
    script.refuse["sign"] = ClickError("human.request_state", "the request is submitted")
    host_check.pending[REQ] = "submitted"
    got = sign_lost(client, token, stub)
    err = got.json()["error"]
    assert err["code"] == "intent.done"
    assert err["signed_at"] == "2026-10-13T15:20:00Z"
    assert err["op"] == "log" and "host text" in err["summary"]


def test_binding_conflict_with_open_request_keeps_expiry(client, token, stub, script, host_check):
    script.refuse["prepare"] = ClickError("idem.conflict", "different device")
    host_check.listing = [
        {"id": REQ, "op": "log", "key": "DEMO-0001", "state": "bound", "expires_at": "2026-10-13T15:30:00Z"}
    ]
    got = prepare(client, token, stub, comment(1)).json()["error"]
    assert got["code"] == "intent.open" and "15:30:00Z" in got["message"]


@pytest.mark.parametrize("local", [False, True])
def test_definite_retry_failure_preserves_prior_unknown_outcome(
    client, token, stub, script, gateway, monkeypatch, local
):
    prepare(client, token, stub, comment(1))
    script.refuse["sign"] = ClickError("internal", "unknown outcome")
    assert sign_lost(client, token, stub).json()["error"]["reason"] == "uncertain"
    if local:

        def failed_setup():
            raise ValueError("before dispatch")

        monkeypatch.setattr(gateway.click, "_ensure", failed_setup)
    else:
        script.refuse["sign"] = ClickError("human.click_refused", "short", reason="too_soon")
    assert sign_lost(client, token, stub).status_code != 200
    assert client.app.state.handoffs.is_unsettled(REQ) == stub.ws


@pytest.mark.parametrize("status", [None, [], "invalid", 1])
def test_non_dict_status_closes_new_signer(gateway, monkeypatch, status):
    from orch_dashboard.errors import HostError

    child = Fake(lambda op, args: status)
    monkeypatch.setattr(gateway.click, "_spawn", lambda: child)
    with pytest.raises(HostError) as exc:
        gateway.click.call("sign", {})
    assert exc.value.error["code"] == "click.unavailable"
    assert child.closed and gateway.click.client is None
    assert [op for op, _ in child.calls] == ["status"]


def test_failed_submit_state_never_reports_signed(client, token, stub, script, host_check, monkeypatch):
    prepare(client, token, stub)
    original = host_check.human

    def failed(ws, op, args, **kw):
        if op == "human.pending" and "request_id" in args:
            return {
                "requests": [
                    {
                        "id": REQ,
                        "state": "submitted",
                        "seq": None,
                        "submitted_at": "2026-10-13T15:20:00Z",
                        "error": {"code": "gate.stale"},
                    }
                ]
            }
        return original(ws, op, args, **kw)

    monkeypatch.setattr(client.app.state, "human", failed)
    response = client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "intent.failed"
    assert response.json()["error"]["reason"] == "gate.stale"


@pytest.mark.parametrize("retry", [0, 3])
def test_failed_prepare_bumps_idem_for_next_attempt(client, token, stub, script, host_check, monkeypatch, retry):
    body = {**approve(), "retry": retry}
    first = prepare(client, token, stub, body)
    assert first.status_code == 200, first.text
    rid = first.json()["request_id"]
    script.states[rid] = "submitted"
    original = host_check.human

    def failed(ws, op, args, **kw):
        if op == "human.pending" and args.get("request_id") == rid:
            return {"requests": [{"id": rid, "state": "submitted", "seq": None, "error": {"code": "gate.stale"}}]}
        return original(ws, op, args, **kw)

    monkeypatch.setattr(client.app.state, "human", failed)
    response = prepare(client, token, stub, body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "intent.failed"
    assert response.json()["error"]["reason"] == "gate.stale"

    again = prepare(client, token, stub, body)
    assert again.status_code == 200, again.text
    assert again.json()["request_id"] != rid
    pending = prepare(client, token, stub, body)
    assert pending.status_code == 200 and pending.json() == again.json()
    keys = [args["idem"] for op, args in script.clients[0].calls if op == "prepare"]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    assert keys[3] == keys[2]
    assert script.prepared == 2


def test_concurrent_uncertain_sign_remarks_after_definite_failure(client, token, stub, script, monkeypatch):
    prepare(client, token, stub, comment(1))
    handoffs = client.app.state.handoffs
    first_entered, second_marked, first_finished = (threading.Event() for _ in range(3))
    original_begin = handoffs.begin_sign
    marks = 0

    def begin(ws, rid, expires):
        nonlocal marks
        original_begin(ws, rid, expires)
        marks += 1
        if marks == 2:
            second_marked.set()

    monkeypatch.setattr(handoffs, "begin_sign", begin)
    calls = 0

    def interleaved(op, args):
        nonlocal calls
        if op != "sign":
            return script.handler(op, args)
        calls += 1
        if calls == 1:
            first_entered.set()
            assert second_marked.wait(5)
            raise ClickError("human.click_refused", "short", reason="too_soon")
        assert first_finished.wait(5)
        raise ClickError("internal", "unknown outcome")

    script.clients[0].handler = interleaved
    out = {}

    def sign(name):
        out[name] = sign_lost(client, token, stub)

    first = threading.Thread(target=sign, args=("first",))
    second = threading.Thread(target=sign, args=("second",))
    first.start()
    try:
        assert first_entered.wait(5)
        second.start()
        first.join(10)
        assert not first.is_alive()
        assert out["first"].json()["error"]["reason"] == "too_soon"
        assert second_marked.is_set()
        assert handoffs.is_unsettled(REQ) == stub.ws  # a definite failure never clears while another sign is in flight
    finally:
        first_finished.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not second.is_alive()
    assert out["second"].json()["error"]["reason"] == "uncertain"
    assert handoffs.is_unsettled(REQ) == stub.ws


@pytest.mark.parametrize(
    "steps, marked",
    [
        # (call, outcome) in the order the calls finish; both calls began before either finished
        ((("a", "definite"), ("b", "uncertain")), True),
        ((("b", "uncertain"), ("a", "definite")), True),  # F-2: the reverse order must not clear the unknown outcome
        ((("a", "definite"), ("b", "definite")), False),  # two definite failures: nothing is at the host
        ((("a", "uncertain"), ("b", "definite")), True),
        ((("a", "ok"), ("b", "definite")), False),
    ],
)
def test_a_definite_sign_failure_clears_the_mark_only_with_nothing_in_flight_or_unknown(steps, marked):
    from orch_dashboard.signing import Handoffs

    handoffs = Handoffs()
    until = handoffs.clock() + 100
    handoffs.begin_sign("ws", "hr_x", until)
    handoffs.begin_sign("ws", "hr_x", until)
    for _call, outcome in steps:
        handoffs.end_sign("ws", "hr_x", until, outcome)
    assert (handoffs.is_unsettled("hr_x") == "ws") is marked
    assert handoffs.signing == {}


def test_an_unknown_outcome_ends_only_when_the_host_answer_is_known():
    from orch_dashboard.signing import Handoffs

    handoffs = Handoffs()
    until = handoffs.clock() + 100
    handoffs.begin_sign("ws", "hr_x", until)
    handoffs.end_sign("ws", "hr_x", until, "uncertain")
    handoffs.begin_sign("ws", "hr_x", until)  # a retry that fails definitely keeps the earlier unknown outcome marked
    handoffs.end_sign("ws", "hr_x", until, "definite")
    assert handoffs.is_unsettled("hr_x") == "ws"
    handoffs.settled("hr_x")  # the host says submitted (or ended)
    handoffs.begin_sign("ws", "hr_x", until)
    handoffs.end_sign("ws", "hr_x", until, "definite")
    assert handoffs.is_unsettled("hr_x") is None


# -- M7b: the runner grant, a click request in the deposit-only grant mode (D-P2-27, section 0.8.6 item 6) ------------

DEPOSIT_LINE = "The secret goes to your orch runner on this machine and is never shown"
DEPOSIT_RISK = "Whoever controls this dashboard could use this access while it lasts. You can stop it any time."
SECRET = "demo-not-a-real-secret-tail"
GRANT = "gr_01M4Y46B485AXD4CBEJZ15Z1TV"


def runner_grant(**kw):
    return {"action": "runner_grant", **kw}


GRANT_WS = "0" * 32  # set by the `deposits` fixture: the stub's workspace id


def grant_view(**over):
    view = {
        "risk": RISK,
        "op": "grant",
        "workspace": {"id": GRANT_WS, "name": "billing", "prefix": "DEMO"},
        "fingerprint": "0123456789ab",
        "lines": ["grant for Demo Owner", "", DEPOSIT_RISK, DEPOSIT_LINE],
        "headline": "Give your orch runner agent access for 8 hours",
        "risk_lines": [DEPOSIT_RISK],
        "deposit": True,
        "deposit_line": DEPOSIT_LINE,
        "confirm": {"kind": "click"},
    }
    return {"view": {**view, **over}, "view_digest": HASH}


@pytest.fixture
def deposits(script, monkeypatch, host_check):
    """A contract-4 signer whose `sign` of a grant answers like core's: the deposit fields, plus things that must never
    leave this server (the host's `result` with a secret in it, and an unknown field)."""
    global GRANT_WS
    GRANT_WS = script.ws
    host_check.verified = {"fingerprint": "0123456789ab"}
    script.sign_answer = {
        "result": {"data": {"grant": GRANT, "secret": f"{GRANT}.{SECRET}"}, "seq": 9},
        "duplicate": False,
        "deposit": "confirmed",
        "deposit_id": "0" * 25 + "1",
        "grant": GRANT,
        "auth": {"grant": f"{GRANT}.{SECRET}"},
        "ORCH_GRANT": SECRET,
    }
    base = script.handler

    def handler(op, args):
        if op == "sign" and script.view["view"].get("op") == "grant":
            script.states[args["request_id"]] = "submitted"
            return dict(script.sign_answer)
        return base(op, args)

    script.handler = handler
    script.view = grant_view()
    return script


def test_runner_grant_prepares_a_deposit_grant_with_hours_and_scope_only(client, token, stub, deposits):
    res = prepare(client, token, stub, runner_grant(hours=12, scope="all"))
    assert res.status_code == 200, res.text
    sent = [c for c in deposits.clients[0].calls if c[0] == "prepare"][0][1]
    assert sent["op"] == "grant" and sent["args"] == {"hours": 12, "scope": "all"} and sent["deposit"] is True
    assert sent["idem"].startswith("a_")
    prepare(client, token, stub, runner_grant())
    assert [c for c in deposits.clients[0].calls if c[0] == "prepare"][1][1]["args"] == {
        "hours": 8,
        "scope": "workable",
    }


@pytest.mark.parametrize(
    "body",
    [
        runner_grant(hours=169),
        runner_grant(hours=0),
        runner_grant(hours="8"),
        runner_grant(scope="mine"),
        runner_grant(label="runner"),
        runner_grant(verbs="agent"),
        runner_grant(deposit=True),
        runner_grant(secret="x"),
    ],
)
def test_runner_grant_bodies_are_closed(client, token, stub, deposits, body):
    assert prepare(client, token, stub, body).status_code == 400
    assert [c for cl in deposits.clients for c in cl.calls if c[0] == "prepare"] == []


def test_runner_grant_view_is_checked_against_the_callers_grant(client, token, stub, deposits, host_check):
    prepare(client, token, stub, runner_grant())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == deposits.view
    assert host_check.checks[-1]["expect_op"] == "grant" and host_check.checks[-1]["expect_events"] is not None


@pytest.mark.parametrize(
    "over",
    [
        {"deposit": False},
        {"deposit_line": "The secret is shown here"},
        {"lines": ["grant", DEPOSIT_LINE]},  # no risk sentence
        {"lines": ["grant", DEPOSIT_RISK]},  # no deposit line in the lines
        {"confirm": {"kind": "word", "word": "DEMO~0a1b2c3d/GRANT"}},  # a typed word is no longer a confirmation
        {"confirm": {}},
        # review L3: another workspace
        {"workspace": {"id": "f" * 32, "name": "other", "prefix": "DEMO"}},
        # review I1: a View of another record than the one verified
        {"fingerprint": "ffffffffffff"},
    ],
)
def test_a_grant_view_without_the_deposit_parts_is_not_shown(client, token, stub, deposits, over):
    deposits.view = grant_view(**over)
    prepare(client, token, stub, runner_grant())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_a_view_of_another_op_that_claims_a_deposit_is_not_shown(client, token, stub, script):
    script.view = {"view": {**script.view["view"], "deposit": True}, "view_digest": HASH}
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_the_grant_sign_answer_is_the_deposit_fields_only_and_never_the_secret(client, token, stub, deposits):
    prepare(client, token, stub, runner_grant())
    body = {"request_id": REQ, "view_digest": HASH}
    res = client.post(url(stub, "sign"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == {
        "state": "submitted",
        "duplicate": False,
        "deposit": "confirmed",
        "deposit_id": "0" * 25 + "1",
        "grant": GRANT,
    }
    assert SECRET not in res.text and "auth" not in res.text and "result" not in res.json()


@pytest.mark.parametrize(
    ("answer", "out"),
    [
        ({"deposit": "rejected", "deposit_reason": "grant.expiring"}, {"deposit_reason": "grant.expiring"}),
        ({"deposit": "rejected", "deposit_reason": "a host sentence"}, {"deposit_reason": "other"}),
        ({"deposit": "rejected"}, {"deposit": "rejected", "deposit_reason": "other"}),
        ({"deposit": "unknown", "deposit_reason": "grant.verbs"}, {"deposit": "unknown"}),
        ({"deposit": "maybe"}, {"deposit": "unknown"}),
        ({"deposit_id": "not-a-ulid"}, {"deposit_id": None}),
        ({"deposit_id": "0" * 25 + "2"}, {"deposit_id": None}),  # not this request's
        ({"grant": f"{GRANT}.{SECRET}"}, {"grant": None}),
    ],
)  # fmt: skip
def test_each_deposit_field_is_checked(client, token, stub, deposits, answer, out):
    deposits.sign_answer = {**deposits.sign_answer, **answer}
    prepare(client, token, stub, runner_grant())
    body = {"request_id": REQ, "view_digest": HASH}
    got = client.post(url(stub, "sign"), json=body, headers=hdr(token)).json()
    for key, value in out.items():
        assert got[key] == value
    assert ("deposit_reason" in got) is (got["deposit"] == "rejected")
    assert SECRET not in repr(got)


def test_expected_admits_only_the_callers_runner_grant():
    from orch_dashboard.signing import RunnerGrant, expected

    me = "p_" + "1" * 32
    good = {
        "type": "grant.issued",
        "label": "runner",
        "verbs": "agent",
        "hours": 8,
        "scope": "workable",
        "actor": {"kind": "person", "id": me, "device": None},
    }
    check = expected(RunnerGrant(action="runner_grant"), me)
    assert check([good])
    for bad in (
        {"label": None},
        {"label": "mine"},
        {"verbs": ["log"]},
        {"verbs": "all"},
        {"hours": 9},
        {"hours": 24},
        {"scope": "all"},
        {"type": "grant.revoked"},
        {"actor": {"kind": "person", "id": "p_" + "2" * 32, "device": None}},
        {"actor": {"kind": "agent", "id": me}},
    ):
        assert not check([{**good, **bad}]), bad
    assert not check([good, good])
    assert not expected(RunnerGrant(action="runner_grant"), None)([good])
    assert not check([{**good, "extra": 1}])  # review I2: no field beyond the grant.issued shape
    twelve = expected(RunnerGrant(action="runner_grant", hours=12, scope="all"), me)
    assert twelve([{**good, "hours": 12, "scope": "all"}]) and not twelve([{**good, "hours": 13, "scope": "all"}])
    # B8 Extend: the new runner grant, then the revoke of exactly the named grant ("extended"), both by the caller
    old = "gr_" + "0" * 26
    ext = expected(RunnerGrant(action="runner_grant", hours=72, scope="all", extend=old), me)
    actor = good["actor"]
    revoke = {"type": "grant.revoked", "grant": old, "reason": "extended", "actor": actor}
    new = {**good, "hours": 72, "scope": "all"}
    assert ext([new, revoke])
    assert not ext([new])  # the revoke leg is part of what is shown
    assert not ext([new, {**revoke, "grant": "gr_" + "1" * 26}])
    assert not ext([new, {**revoke, "reason": "other"}])
    assert not ext([revoke, new])


# -- the click-to-sign switch (section 0.8.6 item 3a) ----------------------------------------------------------------

PASSPHRASE = "CANARY-passphrase-never-in-a-log"


def switch(client, token, stub, body):
    return client.post(url(stub, "custody"), json=body, headers=hdr(token))


def test_an_older_signer_cannot_switch(client, token, stub, script):
    assert client.get(url(stub, "status"), headers=hdr(token, False)).json()["switchable"] is False


def test_a_signer_with_the_custody_feature_can_switch(client, token, stub, script):
    script.features = ["custody", 7]  # a non-string feature is ignored, not an error
    assert client.get(url(stub, "status"), headers=hdr(token, False)).json()["switchable"] is True


def test_switching_on_sends_the_passphrase_to_the_signer_only(client, token, stub, script, caplog, capfd):
    script.features = ["custody"]
    script.enabled = script.available = False
    caplog.set_level(logging.DEBUG)
    res = switch(client, token, stub, {"on": True, "passphrase": PASSPHRASE})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "custody": "convenience",
        "enabled": True,
        "available": True,
        "risk_ack": None,
        "switchable": True,
    }
    calls = [c for c in script.clients[-1].calls if c[0].startswith("custody")]
    assert calls == [("custody_on", {"workspace_id": stub.ws, "passphrase": PASSPHRASE})]
    out, err = capfd.readouterr()
    assert PASSPHRASE not in caplog.text + out + err + res.text


SEAL_OFFER = {
    "request_id": "hr_" + "0" * 25 + "1",
    "view_digest": HASH,
    "seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "B" + "Q" * 86, "purposes": ["custody.passphrase"]},
    "expires_in": 300,
}
PASS_ENV = {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "epk": "B" + "Q" * 86, "ct": "C" * 363}


@pytest.fixture
def local(monkeypatch):
    from orch_dashboard.auth import Auth

    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "local", raising=False)


def seal_post(client, token, stub):
    return client.post(url(stub, "custody/seal"), json={}, headers=hdr(token))


def sealed_off(**over):
    return {
        "on": False,
        "request_id": SEAL_OFFER["request_id"],
        "view_digest": HASH,
        "sealed": {"custody.passphrase": PASS_ENV},
        **over,
    }


def test_switching_off_is_the_flag_only_and_a_clear_passphrase_is_refused(client, token, stub, script, caplog, capfd):
    """#1198 C12: off without a sealed passphrase is the flag only; a new passphrase in the clear is invalid and the
    signer is never asked; an explicit null is invalid too; nothing is logged or echoed."""
    script.features = ["custody", "sealed_passphrase"]
    res = switch(client, token, stub, {"on": False})
    assert res.status_code == 200 and res.json()["enabled"] is False and res.json()["problem"] == "disabled"
    caplog.set_level(logging.DEBUG)
    for body in ({"on": False, "passphrase": None}, {"on": False, "passphrase": PASSPHRASE}):
        bad = switch(client, token, stub, body)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid.input" and PASSPHRASE not in bad.text
    calls = [c for c in script.clients[-1].calls if c[0].startswith("custody")]
    assert calls == [("custody_off", {"workspace_id": stub.ws})]
    out, err = capfd.readouterr()
    assert PASSPHRASE not in caplog.text + out + err


def test_c12_the_seal_offer_is_relayed_for_an_owner_on_a_local_session(client, token, stub, script, local):
    script.features = ["custody", "sealed_passphrase"]
    script.offer = SEAL_OFFER
    res = seal_post(client, token, stub)
    assert res.status_code == 200 and res.json() == SEAL_OFFER
    assert [c for c in script.clients[-1].calls if c[0] == "custody_off"] == [
        ("custody_off", {"workspace_id": stub.ws, "begin": True})
    ]


def test_c12_the_seal_offer_needs_an_owner(client, token, stub, script, monkeypatch):
    from orch_dashboard import adapters
    from orch_dashboard.auth import Auth

    script.offer = SEAL_OFFER
    script.features = ["custody", "sealed_passphrase"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "local", raising=False)
    real = adapters.members
    monkeypatch.setattr(adapters, "members", lambda d: [m.model_copy(update={"role": "maintainer"}) for m in real(d)])
    assert seal_post(client, token, stub).json()["error"]["code"] == "role.denied"
    assert not any(c[0] == "custody_off" for cl in script.clients for c in cl.calls)


def test_c12_the_seal_offer_needs_a_signer_that_can(client, token, stub, script, local):
    script.offer = SEAL_OFFER
    script.features = ["custody"]
    assert seal_post(client, token, stub).json()["error"]["code"] == "click.contract"
    assert not any(c[0] == "custody_off" for cl in script.clients for c in cl.calls)


@pytest.mark.parametrize(
    "over",
    [
        {"request_id": "x"},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "B" + "Q" * 86, "purposes": ["hub.backup_key"]}},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "short", "purposes": ["custody.passphrase"]}},
        {"extra": 1},
    ],
)
def test_c12_an_odd_offer_from_the_signer_is_not_passed_on(client, token, stub, script, local, over):
    script.features = ["custody", "sealed_passphrase"]
    script.offer = {**SEAL_OFFER, **over}
    assert seal_post(client, token, stub).status_code == 409


def test_c12_off_relays_the_sealed_passphrase_as_ciphertext_only(client, token, stub, script, local, caplog, capfd):
    script.features = ["custody", "sealed_passphrase"]
    caplog.set_level(logging.DEBUG)
    res = switch(client, token, stub, sealed_off())
    assert res.status_code == 200, res.text
    assert [c for c in script.clients[-1].calls if c[0] == "custody_off"] == [
        (
            "custody_off",
            {
                "workspace_id": stub.ws,
                "request_id": SEAL_OFFER["request_id"],
                "view_digest": HASH,
                "sealed": {"custody.passphrase": PASS_ENV},
            },
        )
    ]
    out, err = capfd.readouterr()
    assert PASS_ENV["ct"] not in caplog.text + out + err + res.text


def test_c12_sealed_off_needs_a_passkey_step_up_off_this_computer(client, token, stub, script, monkeypatch):
    from orch_dashboard.auth import Auth

    script.features = ["custody", "sealed_passphrase"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "remote", raising=False)
    res = switch(client, token, stub, sealed_off())
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    assert not any(c[0] == "custody_off" for cl in script.clients for c in cl.calls)


def test_c12_sealed_off_needs_a_signer_that_takes_it(client, token, stub, script, local):
    script.features = ["custody"]  # an older signer would take the passphrase in the clear: never asked
    assert switch(client, token, stub, sealed_off()).json()["error"]["code"] == "click.contract"
    assert not any(c[0] == "custody_off" for cl in script.clients for c in cl.calls)


@pytest.mark.parametrize(
    "body",
    [
        {"on": False, "request_id": SEAL_OFFER["request_id"]},
        {"on": False, "sealed": {"custody.passphrase": PASS_ENV}},
        sealed_off(extra=1),
        sealed_off(sealed={"hub.backup_key": PASS_ENV}),
        sealed_off(sealed={"custody.passphrase": {**PASS_ENV, "v": 2}}),
        sealed_off(sealed={"custody.passphrase": {**PASS_ENV, "ct": "short"}}),
        sealed_off(request_id="nope"),
        {**sealed_off(), "on": True, "passphrase": PASSPHRASE},
    ],
)
def test_c12_sealed_bodies_are_closed(client, token, stub, script, local, body):
    script.features = ["custody", "sealed_passphrase"]
    res = switch(client, token, stub, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert not any(c[0].startswith("custody") for cl in script.clients for c in cl.calls)


@pytest.mark.parametrize(
    "body",
    [
        {"on": True},
        {"on": "yes"},
        {"on": True, "passphrase": ""},
        {"on": True, "passphrase": "x" * 1025},
        {},
        {"on": "yes", "passphrase": PASSPHRASE},
        {"on": True, "passphrase": PASSPHRASE, "new_passphrase": PASSPHRASE},
        {"on": False, "passphrase": None},
        {"on": False, "passphrase": ""},
        {"on": True, "passphrase": [PASSPHRASE]},
    ],
)
def test_switch_bodies_are_checked_and_never_echoed(client, token, stub, script, body, caplog, capfd):
    script.features = ["custody"]
    caplog.set_level(logging.DEBUG)
    res = switch(client, token, stub, body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert "x" * 100 not in res.text and "CANARY" not in res.text
    out, err = capfd.readouterr()
    assert "CANARY" not in caplog.text + out + err
    assert not any(c[0].startswith("custody") for cl in script.clients for c in cl.calls)


def test_the_card_names_the_device_that_would_move(client, token, stub, script):
    from orch_dashboard import adapters

    dev = "d_" + "1" * 32
    row = {"custody": "passphrase", "enabled": False, "available": False, "problem": "not_convenience"}
    assert adapters.click_card({**row, "device": dev})["device"] == dev
    assert "device" not in adapters.click_card({**row, "device": "d_<script>"})
    assert "device" not in adapters.click_card(row)


def test_a_terminal_needed_refusal_keeps_its_code(client, token, stub, script):
    script.features = ["custody"]
    script.refuse["custody_on"] = ClickError("custody.terminal_needed", "orch host convenience on --workspace INT")
    res = switch(client, token, stub, {"on": True, "passphrase": PASSPHRASE})
    assert res.json()["error"]["code"] == "custody.terminal_needed" and PASSPHRASE not in res.text


def test_a_signer_without_the_switch_says_update(client, token, stub, script):
    res = switch(client, token, stub, {"on": True, "passphrase": PASSPHRASE})
    assert res.json()["error"]["code"] == "click.contract"
    assert not any(c[0].startswith("custody") for cl in script.clients for c in cl.calls)


def test_only_an_owner_switches(client, token, stub, script, monkeypatch):
    script.features = ["custody"]
    from orch_dashboard import adapters

    real = adapters.members
    monkeypatch.setattr(
        adapters, "members", lambda data: [m.model_copy(update={"role": "maintainer"}) for m in real(data)]
    )
    res = switch(client, token, stub, {"on": False})
    assert res.json()["error"]["code"] == "role.denied"
    assert not any(c[0].startswith("custody") for cl in script.clients for c in cl.calls)


def test_a_wrong_passphrase_keeps_its_code(client, token, stub, script):
    script.features = ["custody"]
    script.refuse["custody_on"] = ClickError("custody.wrong_passphrase", "the passphrase was refused; nothing changed")
    res = switch(client, token, stub, {"on": True, "passphrase": PASSPHRASE})
    assert res.json()["error"]["code"] == "custody.wrong_passphrase"
    script.refuse["custody_on"] = ClickError("rate.limited", "wait", retry_after_ms=5000, retryable=True)
    err = switch(client, token, stub, {"on": True, "passphrase": PASSPHRASE}).json()["error"]
    assert err["code"] == "rate.limited" and err["retry_after_ms"] == 5000


def test_the_switch_takes_a_small_body(client, token, stub, script):
    """orch-9d L3 on #578: /click/custody is a sign-route body (16 KiB), not the general 1 MiB."""
    script.features = ["custody"]
    res = switch(client, token, stub, {"on": True, "passphrase": "x" * 20_000})
    assert res.status_code == 413 and "x" * 100 not in res.text
    assert not any(c[0].startswith("custody") for cl in script.clients for c in cl.calls)


SEQUENCE = {
    "action": "new_sequence",
    "title": "Plan the launch",
    "priority": "high",
    "sections": {"summary": "Why this exists."},
}


def test_a_new_ticket_with_its_text_goes_to_the_signer_as_one_sequence(client, token, stub, script):
    script.features = ["custody", "sequence"]
    res = prepare(client, token, stub, SEQUENCE)
    assert res.status_code == 200, res.text
    (op, args) = [c for c in script.clients[0].calls if c[0] == "prepare"][0]
    assert args["op"] == "new" and args["args"] == {"title": "Plan the launch", "type": "feature"}
    meta = args["sequence"]
    assert meta["step"] == 0 and meta["of"] == 3 and meta["intent"]["sections"] == {"summary": "Why this exists."}
    assert meta["intent"]["set"] == {"priority": "high"} and meta["digest"].startswith("sha256:")


def test_an_older_signer_is_never_asked_for_a_sequence(client, token, stub, script):
    script.features = ["custody"]  # it would ignore `sequence` and sign the first step only
    res = prepare(client, token, stub, SEQUENCE)
    assert res.status_code >= 400 and "orch-core" in res.text
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"action": "publish_setup", "server": "http://127.0.0.1:8103", "share": "http://127.0.0.1:8104"},
        {"action": "publish_share", "key": "DEMO-0001", "staged_id": "ps_" + "0" * 32},
        {"action": "addon_grant", "name": "publish"},  # turning Publish on is terminal only, however it is asked
        {
            "action": "addon_enable",
            "name": "publish",
            "publish": {"server": "https://files.example.test", "share": "https://links.example.test"},
        },
    ],
)
def test_publish_setup_and_share_are_one_click(client, token, stub, script, host_check, rich, body):
    # #1198 C2: Publish is a click like every other op; the signer is asked and a slot is taken.
    res = client.post(url(stub, "prepare"), json=body, headers=hdr(token))
    # (the stub host lacks some of the ops, so only the old terminal-only refusal is asserted away)
    assert res.json().get("error", {}).get("reason") != "terminal_only", res.text


MARKET = {
    "action": "market_grant",
    "name": "echo",
    "staged": "st_" + "0" * 32,
    "grant": {
        "name": "echo",
        "version": "1.0.0",
        "package_sha256": "sha256:" + "a" * 64,
        "capabilities": ["network"],
        "fields": {},
        "sections": [],
        "artifact_kinds": [],
        "staged": "st_" + "0" * 32,
    },
}


@pytest.fixture
def host_serves_grant(monkeypatch):
    """The test host's welcome does not list addon.grant (a real one does); the column check is not what is tested."""
    from orch_dashboard import signing as handoff

    monkeypatch.setattr(handoff, "WORKSPACE_OPS", tuple(o for o in handoff.WORKSPACE_OPS if o != "addon.grant"))


def test_a_market_install_is_one_click_and_the_signer_gets_the_host_review_material_only(
    client, token, stub, script, rich, host_serves_grant
):
    """V3-D38: the grant of a staged marketplace package goes to the signer like any other action. The server names
    the op and its two arguments; the review (capabilities, marketplace block) is the host's and the signer's."""
    script.features = ["custody", "sequence"]
    res = prepare(client, token, stub, MARKET)
    assert res.status_code == 200, res.text
    (op, args) = [c for c in script.clients[0].calls if c[0] == "prepare"][0]
    assert args["op"] == "addon.grant" and args["args"] == {
        "name": "echo",
        "staged": MARKET["staged"],
        "digest": "sha256:" + "a" * 64,
        "caps": ["network"],
    }
    assert args["sequence"]["intent"]["kind"] == "addon_enable" and "files" not in args
    again = prepare(client, token, stub, MARKET)  # the same stage is the same request
    assert again.json()["request_id"] == res.json()["request_id"] and script.prepared == 1
    other = prepare(
        client,
        token,
        stub,
        {**MARKET, "staged": "st_" + "1" * 32, "grant": {**MARKET["grant"], "staged": "st_" + "1" * 32}},
    )  # a new review is a new request
    assert other.status_code == 200 and script.prepared == 2


def test_a_market_install_for_publish_is_a_click_too(client, token, stub, script, rich, host_serves_grant):
    res = prepare(client, token, stub, {**MARKET, "name": "publish", "grant": {**MARKET["grant"], "name": "publish"}})
    assert res.json().get("error", {}).get("reason") != "terminal_only", res.text


def legacy_agent_sequence(client, token, stub):
    """Retained pre-upgrade request: these relay tests still check progress/result sanitization.

    New requests with agent access are refused; the real signer also refuses signing a retained one.
    """
    from orch_dashboard.signing import NewTicketSequence

    res = prepare(client, token, stub, SEQUENCE)
    assert res.status_code == 200, res.text
    rid = res.json()["request_id"]
    ws, op, _, epoch = client.app.state.clicks.get(rid)
    body = NewTicketSequence.model_validate({**SEQUENCE, "agent": {"hours": 24}})
    client.app.state.clicks.add(ws, rid, op, body, epoch)
    client.app.state.handoffs.note_intent(client.app.state.person_of(ws), rid, body.intent())
    return res


def test_a_stopped_sequence_says_which_ticket_exists_and_that_the_agent_access_is_last(
    client, token, stub, script, host_check, rich, monkeypatch
):
    """U4: the server (never error text) says the ticket landed and only the access is missing."""
    script.features = ["custody", "sequence"]
    res = legacy_agent_sequence(client, token, stub)
    assert res.status_code == 200, res.text
    rid = res.json()["request_id"]
    seen = {}

    def progress(gateway, ws, key, intent, person=None, since=None):
        seen.update(key=key, since=since, agent=intent.get("agent"))
        return {"done": 3, "of": 4}

    monkeypatch.setattr("orch_dashboard.click.sequence_progress", progress)
    host_check.key = "DEMO-0044"
    got = client.post(url(stub, "state"), json={"request_id": rid}, headers=hdr(token)).json()
    assert got["key"] == "DEMO-0044" and got["progress"] == {"done": 3, "of": 4, "last": "grant"}
    assert seen == {"key": "DEMO-0044", "since": "2026-10-19T08:00:00Z", "agent": {"hours": 24, "scope": "workable"}}
    # no key on the host's row yet (nothing created): nothing to report
    host_check.key = None
    plain = client.post(url(stub, "state"), json={"request_id": rid}, headers=hdr(token)).json()
    assert "key" not in plain and "progress" not in plain


def test_a_sequence_that_ends_in_the_agent_access_still_answers_its_ticket_key(client, token, stub, script, host_check):
    """The page starts the agent for exactly the ticket this signature created: its key must reach it when the sequence
    ends in a deposit (the access), as it does when there is none. The deposit fields stay the closed set."""
    script.features = ["custody", "sequence"]
    res = legacy_agent_sequence(client, token, stub)
    assert res.status_code == 200, res.text
    rid = res.json()["request_id"]
    base = script.handler

    def handler(op, args):
        if op == "sign":
            script.states[args["request_id"]] = "submitted"
            return {
                "result": {"key": "DEMO-0044", "seq": 7, "secret": SECRET},
                "duplicate": False,
                "deposit": "confirmed",
                "deposit_id": rid[3:],
                "grant": GRANT,
            }
        return base(op, args)

    script.clients[0].handler = handler
    signed = client.post(url(stub, "sign"), json={"request_id": rid, "view_digest": HASH}, headers=hdr(token))
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["key"] == "DEMO-0044" and body["seq"] == 7 and body["deposit"] == "confirmed"
    assert SECRET not in signed.text and "result" not in body


def test_the_key_comes_from_the_hosts_row_when_the_last_steps_result_has_none(client, token, stub, script, host_check):
    """The deposit's result is the grant's: it carries no ticket key. The host's own row of the request does."""
    script.features = ["custody", "sequence"]
    rid = legacy_agent_sequence(client, token, stub).json()["request_id"]
    base = script.clients[0].handler

    def handler(op, args):
        if op == "sign":
            script.states[args["request_id"]] = "submitted"
            return {
                "result": {"data": {"grant": GRANT}},
                "duplicate": False,
                "deposit": "confirmed",
                "deposit_id": rid[3:],
                "grant": GRANT,
            }
        return base(op, args)

    script.clients[0].handler = handler
    host_check.key = "DEMO-0045"
    signed = client.post(url(stub, "sign"), json={"request_id": rid, "view_digest": HASH}, headers=hdr(token))
    assert signed.status_code == 200, signed.text
    assert signed.json()["key"] == "DEMO-0045" and signed.json()["deposit"] == "confirmed"


def test_an_odd_ticket_key_or_seq_is_not_passed_on(client, token, stub, script, host_check):
    """The key must be a ticket key (from the result and from the host's row alike); seq must be a real integer."""
    script.features = ["custody", "sequence"]
    rid = legacy_agent_sequence(client, token, stub).json()["request_id"]
    base = script.clients[0].handler
    answer = {}

    def handler(op, args):
        if op == "sign":
            script.states[args["request_id"]] = "submitted"
            return {
                "result": answer,
                "duplicate": False,
                "deposit": "confirmed",
                "deposit_id": rid[3:],
                "grant": GRANT,
            }
        return base(op, args)

    script.clients[0].handler = handler
    answer.update(key="demo-1; rm", seq=True)
    host_check.key = "not a key"
    body = client.post(url(stub, "sign"), json={"request_id": rid, "view_digest": HASH}, headers=hdr(token)).json()
    assert "key" not in body and "seq" not in body


# --- R3 part B: pasted screenshots ---------------------------------------------------------------------------------
DIGEST = "ab" * 32
PIC = {
    "name": f"paste-20261019-101500-{DIGEST[:8]}.png",
    "sha256": f"sha256:{DIGEST}",
    "bytes": 321,
    "alt": "the broken page",
}
WITH_PIC = {
    **SEQUENCE,
    "sections": {"summary": "Why.", "context": f"Look: ![the broken page](artifact:{PIC['name']})"},
    "images": [PIC],
}


def _all_keys(node):
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _all_keys(v)}
    if isinstance(node, list | tuple):
        return {k for v in node for k in _all_keys(v)}
    return set()


def test_a_new_ticket_with_a_picture_sends_the_intent_with_its_digest_and_never_the_bytes(client, token, stub, script):
    script.features = ["custody", "sequence", "images"]
    res = prepare(client, token, stub, WITH_PIC)
    assert res.status_code == 200, res.text
    (op, args) = [c for c in script.clients[0].calls if c[0] == "prepare"][0]
    meta = args["sequence"]
    assert meta["of"] == 5  # new, set, the picture, two sections
    assert meta["intent"]["images"] == [PIC]
    # Structural, not a repr substring: random ids/digests are hex and can contain "b64" (#1114).
    assert "files" not in args and "b64" not in _all_keys(args)


def test_a_signer_that_cannot_hold_pictures_is_never_asked_for_one(client, token, stub, script):
    script.features = ["custody", "sequence"]
    res = prepare(client, token, stub, WITH_PIC)
    assert res.status_code >= 400 and "orch-core" in res.text
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_a_picture_no_section_shows_is_refused_before_the_signer(client, token, stub, script):
    script.features = ["custody", "sequence", "images"]
    res = prepare(client, token, stub, {**WITH_PIC, "sections": {"summary": "No picture here."}})
    assert res.status_code == 400 and "shown by a written section" in res.text
    assert [c for cl in script.clients for c in cl.calls if c[0] == "prepare"] == []


def test_a_picture_chunk_goes_to_the_signer_and_nowhere_else(client, token, stub, script):
    script.features = ["custody", "sequence", "images"]
    body = {"sha256": f"sha256:{DIGEST}", "size": 3, "offset": 0, "b64": "AAEC", "last": True}
    res = client.post(url(stub, "images"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    (call,) = [c for cl in script.clients for c in cl.calls if c[0] == "stash"]
    assert call[1]["workspace_id"] == script.ws and call[1]["b64"] == "AAEC" and call[1]["last"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"size": 5 * 1024 * 1024 + 1},
        {"b64": "not base64!"},
        {"b64": "A" * 720_004},
        {"sha256": "sha256:short"},
        {"extra": 1},
    ],
)
def test_a_picture_chunk_is_a_closed_body(client, token, stub, script, bad):
    script.features = ["custody", "sequence", "images"]
    body = {"sha256": f"sha256:{DIGEST}", "size": 3, "offset": 0, "b64": "AAEC", "last": True, **bad}
    res = client.post(url(stub, "images"), json=body, headers=hdr(token))
    assert res.status_code in (400, 413, 422)
    assert not [c for cl in script.clients for c in cl.calls if c[0] == "stash"]


def test_pictures_need_a_signer_that_holds_them(client, token, stub, script):
    script.features = ["custody", "sequence"]
    body = {"sha256": f"sha256:{DIGEST}", "size": 3, "offset": 0, "b64": "AAEC", "last": True}
    res = client.post(url(stub, "images"), json=body, headers=hdr(token))
    assert res.status_code >= 400 and "orch-core" in res.text


# -- #1198 C7: agent access whose secret is shown once in the page, sealed to its own key -------------

PAGE_KEY = "B" + "A" * 86  # 87 base64url characters: the shape of a P-256 point; the server never decodes it
REVEAL_LINE = "The secret is shown once, on this page, only to you. orch keeps only a fingerprint of it"
REVEAL_RISK = "The secret will be shown once in this browser. Anyone who controls this dashboard could read it."
ENVELOPE = {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "epk": "C" + "A" * 86, "ct": "D" * 342}


def agent_access(**kw):
    return {"action": "agent_access", "reveal_key": PAGE_KEY, **kw}


def reveal_view(**over):
    import hashlib

    seal = {
        "v": 1,
        "alg": "P256-HKDF-SHA256-A256GCM",
        "purposes": ["grant.secret"],
        "for": hashlib.sha256(PAGE_KEY.encode()).hexdigest(),
    }
    view = {
        "risk": RISK,
        "op": "grant",
        "workspace": {"id": GRANT_WS, "name": "billing", "prefix": "DEMO"},
        "fingerprint": "0123456789ab",
        "lines": ["grant for Demo Owner", "", REVEAL_RISK, REVEAL_LINE],
        "headline": "Give agents access for 2 hours",
        "risk_lines": [REVEAL_RISK, "Agents get this authority for 2 hours, until it ends or you stop it."],
        "seal": seal,
        "confirm": {"kind": "click"},
    }
    return {"view": {**view, **over}, "view_digest": HASH}


@pytest.fixture
def reveals(script, monkeypatch, host_check):
    global GRANT_WS
    GRANT_WS = script.ws
    script.features = ["custody", "reveal"]
    host_check.verified = {"fingerprint": "0123456789ab"}
    script.sign_answer = {
        "result": {"data": {"grant": GRANT, "secret": f"{GRANT}.{SECRET}"}, "seq": 9},
        "duplicate": False,
        "reveal": {"purpose": "grant.secret", "envelope": dict(ENVELOPE)},
        "auth": {"grant": f"{GRANT}.{SECRET}"},
    }
    base = script.handler

    def handler(op, args):
        if op == "sign" and script.view["view"].get("op") == "grant":
            script.states[args["request_id"]] = "submitted"
            return dict(script.sign_answer)
        return base(op, args)

    script.handler = handler
    script.view = reveal_view()
    return script


def sent_of(script, op):
    return [c for cl in script.clients for c in cl.calls if c[0] == op]


def test_agent_access_sends_the_page_key_at_prepare_and_the_grant_args_only(client, token, stub, reveals):
    res = prepare(client, token, stub, agent_access(hours=24, scope="all", label="my tool"))
    assert res.status_code == 200, res.text
    sent = sent_of(reveals, "prepare")[0][1]
    assert sent["op"] == "grant" and sent["args"] == {"hours": 24, "scope": "all", "label": "my tool"}
    assert sent["reveal_key"] == PAGE_KEY and "deposit" not in sent


@pytest.mark.parametrize(
    "body",
    [
        agent_access(hours=169),
        agent_access(label="runner"),
        agent_access(label="x" * 41),
        agent_access(verbs="log"),
        agent_access(runner=True),
        agent_access(reveal_key="short"),
        {"action": "agent_access"},  # no key
    ],
)
def test_agent_access_bodies_are_closed(client, token, stub, reveals, body):
    assert prepare(client, token, stub, body).status_code == 400
    assert sent_of(reveals, "prepare") == []


def test_agent_access_needs_a_signer_that_can_seal(client, token, stub, reveals):
    reveals.features = ["custody"]
    res = prepare(client, token, stub, agent_access())
    assert res.status_code != 200 and sent_of(reveals, "prepare") == []


@pytest.mark.parametrize(
    "over",
    [
        {"seal": None},
        {"seal": {"v": 1, "alg": "x", "purposes": ["grant.secret"], "for": "0" * 64}},  # another key
        {"seal": {"v": 1, "alg": "x", "purposes": ["grant.secret"], "for": "0" * 16}},  # a truncated fingerprint
        {"seal": {"v": 1, "alg": "x", "purposes": ["hub.backup_key"], "for": "x"}},
        {"lines": ["grant", REVEAL_LINE]},  # no risk line
        {"lines": ["grant", REVEAL_RISK]},
        {"deposit": True},
        {"confirm": {}},
        {"workspace": {"id": "f" * 32, "name": "other", "prefix": "DEMO"}},
        {"fingerprint": "ffffffffffff"},
    ],
)
def test_an_agent_access_view_without_the_reveal_parts_is_not_shown(client, token, stub, reveals, over):
    reveals.view = reveal_view(**over)
    prepare(client, token, stub, agent_access())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_a_view_of_another_action_that_claims_a_seal_is_not_shown(client, token, stub, script):
    script.view = {"view": {**script.view["view"], "seal": reveal_view()["view"]["seal"]}, "view_digest": HASH}
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_the_agent_access_view_is_shown_as_the_signer_made_it(client, token, stub, reveals):
    prepare(client, token, stub, agent_access())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == reveals.view


def test_the_agent_access_sign_answer_is_the_grant_id_and_the_sealed_envelope_only(client, token, stub, reveals):
    prepare(client, token, stub, agent_access())
    res = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY}, headers=hdr(token)
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "state": "submitted",
        "duplicate": False,
        "grant": GRANT,
        "reveal": {"purpose": "grant.secret", "envelope": ENVELOPE},
    }
    assert SECRET not in res.text and "auth" not in res.text and "result" not in res.json()
    assert sent_of(reveals, "sign")[0][1]["reveal_key"] == PAGE_KEY


def test_a_swapped_reveal_key_never_reaches_the_signer(client, token, stub, reveals):
    prepare(client, token, stub, agent_access())
    other = "E" + "A" * 86
    for key in (other, None):
        body = {"request_id": REQ, "view_digest": HASH} | ({"reveal_key": key} if key else {})
        res = client.post(url(stub, "sign"), json=body, headers=hdr(token))
        assert res.status_code in (400, 409), res.text
    assert sent_of(reveals, "sign") == []


def test_reveal_key_is_refused_on_any_other_sign(client, token, stub, script):
    prepare(client, token, stub)
    res = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY}, headers=hdr(token)
    )
    assert res.status_code in (400, 409) and sent_of(script, "sign") == []


@pytest.mark.parametrize(
    "bad",
    [
        {"v": 2},
        {"alg": "x"},
        {"epk": "short"},
        {"ct": ""},
        {"extra": 1},
    ],
)
def test_a_malformed_envelope_is_dropped_not_repaired_and_the_secret_is_never_passed(client, token, stub, reveals, bad):
    reveals.sign_answer = {
        **reveals.sign_answer,
        "reveal": {"purpose": "grant.secret", "envelope": {**ENVELOPE, **bad}},
    }
    prepare(client, token, stub, agent_access())
    res = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY}, headers=hdr(token)
    )
    assert res.json()["reveal"] is None and res.json()["grant"] == GRANT
    assert SECRET not in res.text


def test_the_server_logs_never_hold_the_envelope_or_the_secret(client, token, stub, reveals, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    prepare(client, token, stub, agent_access())
    client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY}, headers=hdr(token)
    )
    assert SECRET not in caplog.text and ENVELOPE["ct"] not in caplog.text


def test_expected_admits_only_the_callers_agent_access():
    from orch_dashboard.signing import AgentAccess, expected

    me = "p_" + "1" * 32
    good = {
        "type": "grant.issued",
        "verbs": "agent",
        "hours": 8,
        "scope": "workable",
        "label": "tool",
        "actor": {"kind": "person", "id": me, "device": None},
    }
    check = expected(AgentAccess(action="agent_access", reveal_key=PAGE_KEY, label="tool"), me)
    assert check([good])
    for bad in ({"label": "runner"}, {"label": None}, {"verbs": ["log"]}, {"hours": 9}, {"scope": "all"}, {"extra": 1}):
        assert not check([{**good, **bad}]), bad
    assert not check([good, good])


def test_a_new_page_key_is_a_new_request_and_the_same_key_the_same_one():
    from orch_dashboard.signing import AgentAccess

    other = "E" + "A" * 86
    a = AgentAccess(action="agent_access", reveal_key=PAGE_KEY)
    b = AgentAccess(action="agent_access", reveal_key=other)
    from orch_dashboard.signing import intent_idem

    def idem(body):
        return intent_idem(
            "p_" + "1" * 32,
            "0" * 32,
            "grant",
            {"hours": 8, "scope": "workable"},
            {"reveal_key": body.reveal_key},
            None,
            0,
        )

    assert idem(a) == idem(a) and idem(a) != idem(b)


def test_the_page_key_fingerprint_matches_the_golden_vectors_shared_with_core_and_the_page():
    import json
    from pathlib import Path

    from orch_dashboard.click import key_fingerprint

    path = Path(__file__).resolve().parents[4] / "core/tests/click_reveal_fingerprint.json"
    cases = json.loads(path.read_text())["cases"]
    assert cases
    for case in cases:
        assert len(case["fingerprint"]) == 64 and key_fingerprint(case["key"]) == case["fingerprint"]


def test_a_grant_reveal_is_signed_only_with_a_step_up_on_any_session_that_is_not_a_local_one(
    client, token, stub, reveals, auth
):
    """A remote or unknown session kind prepares the review (a sheet, nothing secret) but never signs the reveal
    without a passkey step-up (R1-6)."""
    assert prepare(client, token, stub, agent_access()).status_code == 200
    for kind in ("remote", "hub", "passkey", None):
        auth.kinds[token] = kind
        res = client.post(
            url(stub, "sign"),
            json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY},
            headers=hdr(token),
        )
        assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required", (kind, res.text)
    assert sent_of(reveals, "sign") == []
    auth.kinds[token] = "local"
    res = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "reveal_key": PAGE_KEY}, headers=hdr(token)
    )
    assert res.status_code == 200, res.text


def test_c21_a_missing_key_at_sign_is_a_definite_refusal_not_uncertain(client, token, stub, script):
    prepare(client, token, stub, comment(1))
    msg = "There is no signing key on this computer. Turn One click off and on again, or sign in a terminal."
    script.refuse["sign"] = ClickError("custody.no_key", msg)
    res = sign_lost(client, token, stub)
    err = res.json()["error"]
    assert res.status_code == 409 and err["code"] == "custody.no_key" and err.get("reason") != "uncertain"
    assert err["message"] == msg
    assert client.app.state.handoffs.is_unsettled(REQ) is None  # nothing is unsettled: it can be tried again
    del script.refuse["sign"]
    assert sign_lost(client, token, stub).status_code == 200


# -- #1198 C10: hub steps by click, with the backup key sealed to the signer ----------------------------

BACKUP_LINE = "This step also needs your backup key. You type it once here; orch does not keep it"
SIGNER_PK = "B" + "Q" * 86
BACKUP_ENV = {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "epk": "C" + "A" * 86, "ct": "D" * 363}
HUB_ID = "a" * 32


def hub_join(**kw):
    return {"action": "hub_join", "hub_card": '{"o":{"hub_id":"' + HUB_ID + '"}}', "hub_id": HUB_ID, **kw}


def backup_view(**over):
    seal = {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": SIGNER_PK, "purposes": ["hub.backup_key"]}
    view = {
        "risk": RISK,
        "op": "hub.join",
        "workspace": {"id": GRANT_WS, "name": "billing", "prefix": "DEMO"},
        "fingerprint": "0123456789ab",
        "lines": ["join the hub", "", BACKUP_LINE],
        "headline": "Join a hub",
        "risk_lines": [HUB_SELECTIVE_RISK],
        "seal": seal,
        "backup_key_line": BACKUP_LINE,
        "confirm": {"kind": "click"},
    }
    return {"view": {**view, **over}, "view_digest": HASH}


@pytest.fixture
def backup(script, monkeypatch, host_check):
    global GRANT_WS
    GRANT_WS = script.ws
    script.features = ["custody", "backup_key"]
    host_check.verified = {"fingerprint": "0123456789ab"}
    monkeypatch.setattr(client_login_kind(), "login_kind", lambda self, token: "local", raising=False)
    script.view = backup_view()
    return script


def client_login_kind():
    from orch_dashboard.auth import Auth

    return Auth


def sign_hub(client, token, stub, **body):
    return client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, **body}, headers=hdr(token))


def test_a_hub_join_prepares_the_hub_op_and_the_card_only(client, token, stub, backup):
    res = prepare(client, token, stub, hub_join())
    assert res.status_code == 200, res.text
    sent = sent_of(backup, "prepare")[0][1]
    assert sent["op"] == "hub.join" and sent["args"] == {
        "hub_card": {"o": {"hub_id": HUB_ID}},
        "hub_id": HUB_ID,
        "mode": "selective",
    }
    assert "reveal_key" not in sent and "deposit" not in sent
    left = prepare(client, token, stub, {"action": "hub_leave"})
    assert sent_of(backup, "prepare")[-1][1]["op"] == "hub.leave" and left.status_code in (200, 409)


@pytest.mark.parametrize(
    "body",
    [
        hub_join(mode="maybe"),
        hub_join(hub_id="XYZ"),
        hub_join(hub_card="not json"),
        hub_join(hub_card="[]"),
        hub_join(extra=1),
        {"action": "hub_leave", "hub_id": HUB_ID},
    ],
)
def test_hub_bodies_are_closed(client, token, stub, backup, body):
    assert prepare(client, token, stub, body).status_code == 400
    assert sent_of(backup, "prepare") == []


def test_a_hub_step_needs_a_signer_that_takes_the_backup_key(client, token, stub, backup):
    backup.features = ["custody"]
    res = prepare(client, token, stub, hub_join())
    assert res.status_code != 200 and sent_of(backup, "prepare") == []


@pytest.mark.parametrize(
    "over",
    [
        {"seal": None},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": SIGNER_PK, "purposes": ["custody.passphrase"]}},
        {
            "seal": {
                "v": 1,
                "alg": "P256-HKDF-SHA256-A256GCM",
                "pk": SIGNER_PK,
                "purposes": ["hub.backup_key", "github.token"],
            }
        },
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "purposes": ["hub.backup_key"], "for": "0" * 64}},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "short", "purposes": ["hub.backup_key"]}},
        {"lines": ["join the hub"]},
        {"backup_key_line": "something else"},
        {"deposit": True},
        {"confirm": {}},
        {"workspace": {"id": "f" * 32, "name": "other", "prefix": "DEMO"}},
    ],
)
def test_a_hub_view_without_the_backup_key_parts_is_not_shown(client, token, stub, backup, over):
    backup.view = backup_view(**over)
    prepare(client, token, stub, hub_join())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_a_view_of_another_action_that_asks_for_the_backup_key_is_not_shown(client, token, stub, script):
    script.view = {"view": {**script.view["view"], "seal": backup_view()["view"]["seal"]}, "view_digest": HASH}
    prepare(client, token, stub)
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 409


def test_the_hub_view_is_shown_as_the_signer_made_it(client, token, stub, backup):
    prepare(client, token, stub, hub_join())
    res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert res.json() == backup.view


def test_the_sealed_backup_key_is_relayed_as_it_came_and_not_kept(client, token, stub, backup, caplog):
    prepare(client, token, stub, hub_join())
    with caplog.at_level("DEBUG"):
        res = sign_hub(client, token, stub, sealed={"hub.backup_key": BACKUP_ENV})
    assert res.status_code == 200, res.text
    assert sent_of(backup, "sign")[0][1]["sealed"] == {"hub.backup_key": BACKUP_ENV}
    assert BACKUP_ENV["ct"] not in res.text and BACKUP_ENV["ct"] not in caplog.text
    assert res.json() == {"state": "submitted", "duplicate": False} | {
        k: v for k, v in res.json().items() if k in ("key", "seq")
    }


@pytest.mark.parametrize(
    "sealed",
    [
        {},
        {"hub.backup_key": {**BACKUP_ENV, "extra": 1}},
        {"hub.backup_key": {**BACKUP_ENV, "v": 2}},
        {"hub.backup_key": {**BACKUP_ENV, "alg": "x"}},
        {"hub.backup_key": {**BACKUP_ENV, "ct": "short"}},
        {"hub.backup_key": BACKUP_ENV, "custody.passphrase": BACKUP_ENV},
        {"custody.passphrase": BACKUP_ENV},
        {"hub.backup_key": "plain words typed here"},
    ],
)
def test_a_hub_sign_without_a_well_formed_sealed_key_never_reaches_the_signer(client, token, stub, backup, sealed):
    prepare(client, token, stub, hub_join())
    body = {} if sealed is None else {"sealed": sealed}
    res = sign_hub(client, token, stub, **body)
    assert res.status_code in (400, 409), res.text
    assert sent_of(backup, "sign") == []


def test_a_sealed_value_on_any_other_action_is_refused(client, token, stub, script):
    prepare(client, token, stub)
    res = sign_hub(client, token, stub, sealed={"hub.backup_key": BACKUP_ENV})
    assert res.status_code == 400
    assert sent_of(script, "sign") == []


def test_a_sealed_sign_on_a_non_local_session_never_reaches_the_signer(client, token, stub, backup, monkeypatch):
    prepare(client, token, stub, hub_join())
    monkeypatch.setattr(client_login_kind(), "login_kind", lambda self, token: "remote", raising=False)
    res = sign_hub(client, token, stub, sealed={"hub.backup_key": BACKUP_ENV})
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    assert sent_of(backup, "sign") == []


def test_c17_a_plain_click_resumes_one_click_from_a_local_dashboard(client, token, stub, script, auth):
    script.features = ["custody"]
    script.enabled = script.available = False
    auth.kinds[token] = "local"
    res = switch(client, token, stub, {"on": True, "resume": True})
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is True
    calls = [c for c in script.clients[-1].calls if c[0].startswith("custody")]
    assert calls == [("custody_on", {"workspace_id": stub.ws, "resume": True})]


def test_c17_a_resume_from_any_other_session_needs_a_passkey_step_up(client, token, stub, script, auth):
    script.features = ["custody"]
    script.enabled = script.available = False
    for kind in ("remote", "passkey", None):
        auth.kinds[token] = kind
        res = switch(client, token, stub, {"on": True, "resume": True})
        assert res.json()["error"]["code"] == "passkey.stepup_required", res.text
    assert not any(c[0] == "custody_on" for cl in script.clients for c in cl.calls)


def test_c17_resume_is_on_without_a_passphrase_only(client, token, stub, script, auth):
    script.features = ["custody"]
    auth.kinds[token] = "local"
    sealed = {"request_id": "hr_" + "0" * 26, "view_digest": "0" * 64}
    for body in (
        {"on": False, "resume": True},
        {"on": True, "resume": True, "passphrase": PASSPHRASE},
        {"on": True, "resume": True, **sealed},
    ):
        res = switch(client, token, stub, body)
        assert res.json()["error"]["code"] == "invalid.input" and PASSPHRASE not in res.text
    assert not any(c[0] == "custody_on" for cl in script.clients for c in cl.calls)


# -- #1198 C13: replace this computer's device key ---------------------------------------------------------------------
OLD, NEW = "d_" + "1" * 32, "d_" + "2" * 32
ROTATE_OFFER_FIELDS = {
    "request_id": "hr_" + "0" * 25 + "2",
    "view_digest": HASH,
    "expires_in": 300,
}


def rotate_offer(ws, **over):
    seal = {
        "v": 1,
        "alg": "P256-HKDF-SHA256-A256GCM",
        "pk": "B" + "Q" * 86,
        "purposes": ["device.recovery_code", "custody.passphrase"],
    }
    view = {
        "op": "device.rotate",
        "workspace": {"id": ws, "name": "demo", "prefix": "DEMO"},
        "old": OLD,
        "new": NEW,
        "lines": ["Replace the signing key of this computer for demo.", f"old device: {OLD}", f"new device: {NEW}"],
        "confirm": {"kind": "click"},
        "seal": seal,
    }
    return {**ROTATE_OFFER_FIELDS, "view": {**view, **over}}


def rotate_body(**over):
    return {
        "request_id": ROTATE_OFFER_FIELDS["request_id"],
        "view_digest": HASH,
        "sealed": {"device.recovery_code": PASS_ENV, "custody.passphrase": PASS_ENV},
        **over,
    }


def rotate_calls(script):
    return [c for cl in script.clients for c in cl.calls if c[0] == "rotate"]


def test_c13_the_review_is_relayed_for_an_owner_on_a_local_session(client, token, stub, script, local):
    script.features = ["custody", "device_rotate"]
    script.rotation = rotate_offer(stub.ws)
    res = client.post(url(stub, "rotate/seal"), json={}, headers=hdr(token))
    assert res.status_code == 200 and res.json() == script.rotation
    assert rotate_calls(script) == [("rotate", {"workspace_id": stub.ws, "begin": True})]


def test_c13_begin_and_sign_are_owner_only_need_the_feature_and_a_step_up_off_this_computer(
    client, token, stub, script, monkeypatch
):
    from orch_dashboard import adapters
    from orch_dashboard.auth import Auth

    script.rotation = rotate_offer(stub.ws)
    script.features = ["custody", "device_rotate"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "remote", raising=False)
    res = client.post(url(stub, "rotate"), json=rotate_body(), headers=hdr(token))
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "local", raising=False)
    script.features = ["custody"]
    for path, body in (("rotate/seal", {}), ("rotate", rotate_body())):
        assert client.post(url(stub, path), json=body, headers=hdr(token)).json()["error"]["code"] == "click.contract"
    script.features = ["custody", "device_rotate"]
    real = adapters.members
    monkeypatch.setattr(adapters, "members", lambda d: [m.model_copy(update={"role": "maintainer"}) for m in real(d)])
    for path, body in (("rotate/seal", {}), ("rotate", rotate_body())):
        assert client.post(url(stub, path), json=body, headers=hdr(token)).json()["error"]["code"] == "role.denied"
    assert rotate_calls(script) == []


@pytest.mark.parametrize(
    "over",
    [
        {"op": "approve"},
        {"old": "x"},
        {"new": OLD},
        {"workspace": {"id": "0" * 32}},
        {"confirm": {"kind": "word"}},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "B" + "Q" * 86, "purposes": ["custody.passphrase"]}},
        {
            "seal": {
                "v": 1,
                "alg": "P256-HKDF-SHA256-A256GCM",
                "pk": "short",
                "purposes": ["device.recovery_code", "custody.passphrase"],
            }
        },
    ],
)
def test_c13_an_odd_review_from_the_signer_is_not_passed_on(client, token, stub, script, local, over):
    script.features = ["custody", "device_rotate"]
    script.rotation = rotate_offer(stub.ws, **over)
    assert client.post(url(stub, "rotate/seal"), json={}, headers=hdr(token)).status_code == 409


def test_c13_a_finished_earlier_run_answers_done(client, token, stub, script, local):
    script.features = ["custody", "device_rotate"]
    script.rotation = {"done": True, "old": OLD, "new": NEW, "custody": "passphrase", "enabled": False}
    res = client.post(url(stub, "rotate/seal"), json={}, headers=hdr(token))
    assert res.status_code == 200 and res.json()["done"] is True and res.json()["custody"] == "passphrase"


def test_c13_sign_relays_ciphertext_only(client, token, stub, script, local, caplog, capfd):
    script.features = ["custody", "device_rotate"]
    caplog.set_level(logging.DEBUG)
    res = client.post(url(stub, "rotate"), json=rotate_body(), headers=hdr(token))
    assert res.status_code == 200 and res.json()["done"] is True and res.json()["custody"] == "passphrase"
    assert rotate_calls(script) == [
        (
            "rotate",
            {
                "workspace_id": stub.ws,
                "request_id": ROTATE_OFFER_FIELDS["request_id"],
                "view_digest": HASH,
                "sealed": {"device.recovery_code": PASS_ENV, "custody.passphrase": PASS_ENV},
            },
        )
    ]
    out, err = capfd.readouterr()
    assert PASS_ENV["ct"] not in caplog.text + out + err + res.text


@pytest.mark.parametrize(
    "body",
    [
        rotate_body(extra=1),
        rotate_body(passphrase="CANARY-clear"),
        rotate_body(sealed={"device.recovery_code": PASS_ENV}),
        rotate_body(sealed={"custody.passphrase": PASS_ENV}),
        rotate_body(sealed={"device.recovery_code": PASS_ENV, "custody.passphrase": {**PASS_ENV, "v": 2}}),
        rotate_body(
            sealed={"device.recovery_code": PASS_ENV, "custody.passphrase": PASS_ENV, "hub.backup_key": PASS_ENV}
        ),
        rotate_body(request_id="nope"),
        rotate_body(view_digest="sha256:short"),
    ],
)
def test_c13_sign_bodies_are_closed_and_never_echoed(client, token, stub, script, local, body):
    script.features = ["custody", "device_rotate"]
    res = client.post(url(stub, "rotate"), json=body, headers=hdr(token))
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input" and "CANARY" not in res.text
    assert rotate_calls(script) == []


# -- #1198 C16: the GitHub token, sealed to the signer --------------------------------------------------
GH_OFFER_FIELDS = {"request_id": "hr_" + "0" * 25 + "3", "view_digest": HASH, "expires_in": 300}


def gh_offer(**over):
    seal = {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "B" + "Q" * 86, "purposes": ["github.token"]}
    return {**GH_OFFER_FIELDS, "seal": seal, **over}


def gh_body(**over):
    return {
        "request_id": GH_OFFER_FIELDS["request_id"],
        "view_digest": HASH,
        "sealed": {"github.token": PASS_ENV},
        **over,
    }


def gh_calls(script):
    return [c for cl in script.clients for c in cl.calls if c[0] == "github_token"]


def test_c16_the_offer_is_relayed_for_an_owner(client, token, stub, script, local):
    script.features = ["custody", "github_token"]
    script.github = gh_offer()
    res = client.post(url(stub, "github/seal"), json={}, headers=hdr(token))
    assert res.status_code == 200 and res.json() == script.github
    assert gh_calls(script) == [("github_token", {"workspace_id": stub.ws, "begin": True})]


def test_c16_save_relays_ciphertext_only_and_answers_stored(client, token, stub, script, local, caplog, capfd):
    script.features = ["custody", "github_token"]
    caplog.set_level(logging.DEBUG)
    res = client.post(url(stub, "github"), json=gh_body(), headers=hdr(token))
    assert res.status_code == 200 and res.json() == {"credential": "stored"}
    assert gh_calls(script) == [
        (
            "github_token",
            {
                "workspace_id": stub.ws,
                "request_id": GH_OFFER_FIELDS["request_id"],
                "view_digest": HASH,
                "sealed": {"github.token": PASS_ENV},
            },
        )
    ]
    out, err = capfd.readouterr()
    assert PASS_ENV["ct"] not in caplog.text + out + err + res.text


def test_c16_remove_is_a_plain_click_even_remote(client, token, stub, script, monkeypatch):
    from orch_dashboard.auth import Auth

    script.features = ["custody", "github_token"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "remote", raising=False)
    res = client.post(url(stub, "github/remove"), json={}, headers=hdr(token))
    assert res.status_code == 200 and res.json() == {"credential": "missing"}
    assert gh_calls(script) == [("github_token", {"workspace_id": stub.ws, "remove": True})]


def test_c16_a_remote_session_needs_the_step_up_and_a_local_one_does_not(client, token, stub, script, monkeypatch):
    from orch_dashboard.auth import Auth

    script.features = ["custody", "github_token"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "remote", raising=False)
    res = client.post(url(stub, "github"), json=gh_body(), headers=hdr(token))
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    assert gh_calls(script) == []


def test_c16_owner_only_and_feature_gated(client, token, stub, script, local, monkeypatch):
    from orch_dashboard import adapters

    script.features = ["custody"]
    for path, body in (("github/seal", {}), ("github", gh_body()), ("github/remove", {})):
        assert client.post(url(stub, path), json=body, headers=hdr(token)).json()["error"]["code"] == "click.contract"
    script.features = ["custody", "github_token"]
    real = adapters.members
    monkeypatch.setattr(adapters, "members", lambda d: [m.model_copy(update={"role": "maintainer"}) for m in real(d)])
    for path, body in (("github/seal", {}), ("github", gh_body()), ("github/remove", {})):
        assert client.post(url(stub, path), json=body, headers=hdr(token)).json()["error"]["code"] == "role.denied"
    assert gh_calls(script) == []


@pytest.mark.parametrize(
    "over",
    [
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "B" + "Q" * 86, "purposes": ["custody.passphrase"]}},
        {"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": "short", "purposes": ["github.token"]}},
        {"extra": 1},
    ],
)
def test_c16_an_odd_offer_from_the_signer_is_not_passed_on(client, token, stub, script, local, over):
    script.features = ["custody", "github_token"]
    script.github = gh_offer(**over)
    assert client.post(url(stub, "github/seal"), json={}, headers=hdr(token)).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        gh_body(extra=1),
        gh_body(token="ghp_CANARY" + "a" * 30),
        gh_body(sealed={"github.token": PASS_ENV, "hub.backup_key": PASS_ENV}),
        gh_body(sealed={"custody.passphrase": PASS_ENV}),
        gh_body(sealed={"github.token": {**PASS_ENV, "v": 2}}),
        gh_body(request_id="nope"),
    ],
)
def test_c16_bodies_are_closed_and_never_echoed(client, token, stub, script, local, body):
    script.features = ["custody", "github_token"]
    res = client.post(url(stub, "github"), json=body, headers=hdr(token))
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input" and "CANARY" not in res.text
    assert gh_calls(script) == []


def runner_calls(script):
    return [c for cl in script.clients for c in cl.calls if c[0] == "runner"]


def test_c14_review_then_on_for_an_owner_on_a_local_session(client, token, stub, script, local):
    script.features = ["custody", "runner_switch"]
    res = client.post(url(stub, "runner/review"), json={"on": True}, headers=hdr(token))
    assert res.status_code == 200 and res.json()["view_digest"] == HASH
    assert "Agents may run commands on this computer as you when you start them." in res.json()["view"]["lines"]
    res = client.post(
        url(stub, "runner"), json={"on": True, "terminals": False, "view_digest": HASH}, headers=hdr(token)
    )
    assert res.status_code == 200 and res.json() == {"enabled": True, "terminals": False}
    assert runner_calls(script)[-1] == (
        "runner", {"workspace_id": stub.ws, "on": True, "terminals": False, "view_digest": HASH}
    )  # fmt: skip


def test_c14_on_needs_a_step_up_and_off_is_a_plain_click_anywhere(client, token, stub, script, monkeypatch):
    from orch_dashboard.auth import Auth

    script.features = ["custody", "runner_switch", "passkey_stepup"]
    monkeypatch.setattr(Auth, "login_kind", lambda self, token: "remote", raising=False)
    res = client.post(url(stub, "runner"), json={"on": True, "view_digest": HASH}, headers=hdr(token))
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    assert runner_calls(script) == []
    assert client.post(url(stub, "runner/review"), json={"on": False}, headers=hdr(token)).status_code == 200
    res = client.post(url(stub, "runner"), json={"on": False, "view_digest": HASH}, headers=hdr(token))
    assert res.status_code == 200 and res.json() == {"enabled": False, "terminals": False}


def test_c14_needs_an_owner_the_feature_and_a_digest(client, token, stub, script, local, monkeypatch):
    from orch_dashboard import adapters

    body = {"on": True, "view_digest": HASH}
    script.features = ["custody", "runner_switch"]
    assert (
        client.post(url(stub, "runner"), json={"on": True}, headers=hdr(token)).json()["error"]["code"]
        == "invalid.input"
    )
    real = adapters.members
    monkeypatch.setattr(adapters, "members", lambda d: [m.model_copy(update={"role": "maintainer"}) for m in real(d)])
    for path in ("runner/review", "runner"):
        assert client.post(url(stub, path), json=body, headers=hdr(token)).json()["error"]["code"] == "role.denied"
    assert runner_calls(script) == []


def test_c14_an_older_signer_cannot_switch(client, token, stub, script, local):
    script.features = ["custody"]
    for path in ("runner/review", "runner"):
        res = client.post(url(stub, path), json={"on": True, "view_digest": HASH}, headers=hdr(token))
        assert res.json()["error"]["code"] == "click.contract"
    assert runner_calls(script) == []


def test_c14_a_review_that_is_not_the_one_asked_for_is_not_shown(client, token, stub, script, local, monkeypatch):
    script.features = ["custody", "runner_switch"]
    real = script.handler

    def odd(op, args):
        got = real(op, args)
        if op == "runner" and args.get("review"):
            got["view"]["lines"] = ["Turn on dashboard execution."]  # core's runner line is missing
        return got

    script.handler = odd
    res = client.post(url(stub, "runner/review"), json={"on": True}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_c14_terminals_default_to_off_in_what_is_sent_to_the_signer(client, token, stub, script, local):
    script.features = ["custody", "runner_switch"]
    assert client.post(url(stub, "runner/review"), json={"on": True}, headers=hdr(token)).status_code == 200
    client.post(url(stub, "runner"), json={"on": True, "view_digest": HASH}, headers=hdr(token))
    assert [c[1]["terminals"] for c in runner_calls(script)] == [False, False]


def test_c14_a_review_for_other_terminals_than_asked_is_not_shown(client, token, stub, script, local):
    script.features = ["custody", "runner_switch"]
    real = script.handler

    def odd(op, args):
        got = real(op, args)
        if op == "runner" and args.get("review"):
            got["view"]["terminals"] = True  # the person asked for none
        return got

    script.handler = odd
    res = client.post(url(stub, "runner/review"), json={"on": True, "terminals": False}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"


def test_c16_the_state_is_read_from_the_addon_list_and_never_the_token(client, token, stub, script, local, monkeypatch):
    from orch_dashboard.gateway import Gateway

    real = Gateway.call
    rows: list = []
    monkeypatch.setattr(
        Gateway,
        "call",
        lambda self, ws, op, args=None: {"addons": rows} if op == "addon.list" else real(self, ws, op, args),
    )
    cases = [
        ([{"name": "github", "state": "active", "credential": "stored"}], "stored"),
        ([{"name": "github", "state": "active", "credential": "missing"}], "missing"),
        ([{"name": "github", "state": "active", "credential": None}], None),
        ([{"name": "github", "state": "off", "credential": "stored"}], None),
        ([{"name": "other", "state": "active", "credential": "stored"}], None),
        ([], None),
    ]
    for listed, want in cases:
        rows[:] = listed
        res = client.get(url(stub, "github"), headers=hdr(token))
        assert res.status_code == 200 and res.json() == {"credential": want}, listed


def test_c16_the_state_is_owner_only(client, token, stub, script, local, monkeypatch):
    from orch_dashboard import adapters

    real = adapters.members
    monkeypatch.setattr(adapters, "members", lambda d: [m.model_copy(update={"role": "maintainer"}) for m in real(d)])
    res = client.get(url(stub, "github"), headers=hdr(token))
    assert res.status_code == 403 and res.json()["error"]["code"] == "role.denied"


def test_c16_the_contract_refusal_is_in_plain_words(client, token, stub, script, local):
    script.features = ["custody"]
    res = client.post(url(stub, "github/seal"), json={}, headers=hdr(token))
    msg = res.json()["error"]["message"]
    assert "needs an update" in msg and "signing helper" not in msg and "orch-core" not in msg


def test_hub_steps_get_a_fresh_idem_once_the_hub_state_moved(client, token, stub, backup, monkeypatch):
    """B10/B13: leave, join, mode full, leave is a new request each time; a repeat with no change is not."""
    from orch_dashboard import signing as handoff

    state = {"joined": True, "mode": "selective", "binding_seq": 1, "hub_id": HUB_ID, "binding": "bound"}
    monkeypatch.setattr(handoff, "hub_base", lambda link: {"hub": dict(state)})
    leave = {"action": "hub_leave"}
    assert prepare(client, token, stub, leave).status_code == 200
    prepare(client, token, stub, leave)  # a double click: the same request
    state.update(joined=False)
    assert prepare(client, token, stub, hub_join()).status_code == 200
    state.update(joined=True, binding_seq=2)
    state.update(mode="full")
    assert prepare(client, token, stub, leave).status_code == 200
    keys = [a["idem"] for op, a in sent_of(backup, "prepare")]
    assert keys[0] == keys[1] and keys[3] != keys[0]


def test_hub_base_is_the_log_head_and_the_hub_state():
    from orch.host.journal import CURSOR_PREFIX

    from orch_dashboard.signing import hub_base

    body = base64.urlsafe_b64encode(json.dumps({"e": "E", "j": 7, "p": "P", "r": "R9", "w": "W"}).encode())
    cursor = CURSOR_PREFIX + body.decode().rstrip("=") + ".mac"

    class L:
        def call(self, op, args=None):
            if op == "activity.list":
                return {"items": [], "sub_cursor": cursor}
            return {"joined": True, "mode": "full", "binding_seq": 4, "hub_id": "ab", "binding": "bound", "x": 1}

    want = {"joined": True, "hub_id": "ab", "mode": "full", "binding_seq": 4, "binding": "bound"}
    assert hub_base(L()) == {"head": {"e": "E", "j": 7, "r": "R9"}, "hub": want}


def test_c2_switch_cannot_prepare(client, auth, stub, script):
    from orch_dashboard.policy import LOOPBACK

    switched = auth.open_switch(LOOPBACK, stub.ws)["token"]
    result = prepare(client, switched, stub)
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "session.readonly"
    assert script.prepared == 0


def test_c2_cancelled_request_is_never_signable(client, token, stub, script):
    request_id = prepare(client, token, stub).json()["request_id"]
    cancelled = client.post(url(stub, "cancel"), json={"request_id": request_id}, headers=hdr(token))
    assert cancelled.status_code == 200
    late = client.post(url(stub, "sign"), json={"request_id": request_id, "view_digest": HASH}, headers=hdr(token))
    assert late.status_code == 404
    fresh = prepare(client, token, stub, {**approve(), "retry": 1})
    assert fresh.status_code == 200
    assert fresh.json()["request_id"] != request_id


@pytest.mark.parametrize("minutes,expected_grant", [(None, True), (14, True), (15, False), (16, False)])
def test_d2_host_clock_selects_last_grant(client, token, stub, script, monkeypatch, minutes, expected_grant):
    from datetime import datetime, timedelta

    from orch_dashboard.gateway import Gateway

    script.features = ["custody", "sequence"]
    real = Gateway.call
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)

    def call(self, ws, op, *args, **kwargs):
        if op == "whoami":
            return {
                "runner": {
                    "now": now.isoformat(),
                    "grant": (
                        {"startable": True, "until": (now + timedelta(minutes=minutes)).isoformat()}
                        if minutes is not None
                        else None
                    ),
                }
            }
        return real(self, ws, op, *args, **kwargs)

    monkeypatch.setattr(Gateway, "call", call)
    res = prepare(
        client, token, stub, {**SEQUENCE, "launch": {"harness": "codex", "mode": "refine"}, "agent": {"hours": 8}}
    )
    assert res.status_code == 200, res.text
    args = next(args for op, args in script.clients[0].calls if op == "prepare")
    intent = args["sequence"]["intent"]
    assert intent["launch"] == {"harness": "codex", "mode": "refine"}
    assert ("agent" in intent) is expected_grant
    if expected_grant:
        assert intent["agent"] == {"hours": 8, "scope": "workable"}


def test_d2_valid_access_create_start_view_uses_stored_body(client, token, stub, script, monkeypatch):
    from datetime import datetime, timedelta

    from orch_dashboard.gateway import Gateway

    script.features = ["custody", "sequence"]
    real = Gateway.call
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)

    def call(self, ws, op, *args, **kwargs):
        if op == "whoami":
            return {
                "runner": {
                    "now": now.isoformat(),
                    "grant": ({"startable": True, "until": (now + timedelta(minutes=16)).isoformat()}),
                }
            }
        return real(self, ws, op, *args, **kwargs)

    monkeypatch.setattr(Gateway, "call", call)
    res = prepare(
        client, token, stub, {**SEQUENCE, "launch": {"harness": "codex", "mode": "refine"}, "agent": {"hours": 8}}
    )
    assert res.status_code == 200, res.text
    args = next(args for op, args in script.clients[0].calls if op == "prepare")
    intent = args["sequence"]["intent"]
    assert intent["launch"] == {"harness": "codex", "mode": "refine"}
    assert "agent" not in intent

    # /view must retrieve the body stored by /prepare, after agent was removed.
    # The browser proposed a grant; the test never supplies a stripped body.
    script.view["view"].update(
        op="new",
        headline="Create ticket and start agent",
        risk_lines=[],
        confirm={"kind": "click", "mode": "press"},
    )
    shown = client.post(url(stub, "view"), json={"request_id": res.json()["request_id"]}, headers=hdr(token))
    assert shown.status_code == 200, shown.text
    view = shown.json()["view"]
    assert view["op"] == "new"
    assert view["risk_lines"] == []
    assert view["confirm"] == {"kind": "click", "mode": "press"}


@pytest.mark.parametrize(
    "op,risk,mode,ok",
    [
        ("grant", ["Agents can run as you for 8 hours"], "press", True),
        ("new", ["Agents can run as you for 8 hours"], "press", False),
        ("grant", [], "press", False),
        ("grant", ["Agents can run as you for 8 hours"], "arm", False),
    ],
)
def test_grant_sequence_requires_risk_view_in_press_mode(client, token, stub, script, op, risk, mode, ok):
    script.features = ["custody", "sequence"]
    res = prepare(client, token, stub, {**SEQUENCE, "agent": {"hours": 8}})
    assert res.status_code == 200, res.text
    script.view["view"].update(op=op, risk_lines=risk, confirm={"kind": "click", "mode": mode})
    res = client.post(url(stub, "view"), json={"request_id": res.json()["request_id"]}, headers=hdr(token))
    assert res.status_code == (200 if ok else 409), res.text


@pytest.mark.parametrize(
    "op,risk,ok", [("new", [], True), ("new", ["risk"], False), ("grant", [], False), ("grant", ["risk"], True)]
)
def test_relay_preserves_one_press_allowlist(op, risk, ok):
    from orch_dashboard.click import contract4_view_ok

    assert contract4_view_ok({"op": op, "headline": "Review", "risk_lines": risk}) is ok


@pytest.mark.parametrize("finish", ["cancelled", "not_sent", "unknown"])
def test_d5_hold_relays_session_delay_and_preserves_cancel_and_reconnect(
    client, token, stub, script, auth, host_check, gateway, finish
):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    original = script.clients[0].handler
    outcome = {"state": "held", "deadline_ms": int(time.time() * 1000) + 10000}

    def delayed(op, args):
        if op == "sign":
            assert args["delay_s"] == 10
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            return dict(outcome)
        if op == "status" and args.get("request_id"):
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            return dict(outcome)
        if op == "cancel":
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            outcome["state"] = "cancelled"
            return {"state": "cancelled"}
        return original(op, args)

    script.clients[0].handler = delayed
    signed = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
    )
    assert signed.status_code == 200
    assert signed.json() == {**outcome, "duplicate": False}
    other = other_session(client, auth)
    denied = client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(other))
    assert denied.status_code == 404
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "held"
    prepared = next(args for op, args in script.clients[0].calls if op == "prepare")
    assert prepared["session"] == hashlib.sha256(token.encode()).hexdigest()
    if finish != "unknown":
        outcome["state"] = finish
        route = "cancel" if finish == "cancelled" else "state"
        assert client.post(url(stub, route), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == finish
        host_check.pending[REQ] = "cancelled"
        before = len(script.clients[0].calls)
        client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token))
        assert not any(op == "status" for op, _ in script.clients[0].calls[before:])  # held was pruned
        return
    outcome["state"] = "unknown"
    host_check.pending[REQ] = "submitted"
    gateway.click.restarts += 1  # a stale signer epoch must not sweep a possibly signed hold
    assert client.post(url(stub, "state"), json={"request_id": REQ}, headers=hdr(token)).json()["state"] == "submitted"
    assert not host_check.cancelled


@pytest.mark.parametrize("everywhere", [False, True])
def test_d5_logout_cancels_session_holds(client, token, stub, script, auth, everywhere):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    original = script.clients[0].handler
    pending = set()

    def delayed(op, args):
        if op == "sign":
            pending.add(args["request_id"])
            return {"state": "held", "deadline_ms": 10000}
        if op == "cancel_holds":
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            pending.clear()
            return {"state": "cancelled"}
        return original(op, args)

    script.clients[0].handler = delayed
    result = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
    )
    assert result.status_code == 200
    assert pending == {REQ}
    if everywhere:
        auth.logout_everywhere(kind=auth.login_kind(token))
    else:
        assert client.post("/auth/logout", headers=hdr(token)).status_code == 200
    assert not pending  # the signer's deadline queue has no intent left to sign
    client.app.state.cancel_clicks(token)
    assert not pending  # cancellation by session is idempotent
    assert (
        client.post(
            url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
        ).status_code
        == 401
    )


def test_d5_session_ending_after_authentication_cannot_enqueue(client, token, stub, script, auth, monkeypatch):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    original = auth.valid

    def end_before_send(value, touch=True):
        if not touch:
            auth.logout(value)
        return original(value, touch=touch)

    monkeypatch.setattr(auth, "valid", end_before_send)
    result = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "session.ended"
    assert not any(op == "sign" for op, _ in script.clients[0].calls)


@pytest.mark.parametrize("cancel_failure", [False, True, "timeout"])
def test_d5_lost_sign_reply_logout_destroys_unsigned_queue(client, token, stub, script, auth, gateway, cancel_failure):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    channel = script.clients[0]
    original = channel.handler
    pending = set()
    close = channel.close

    def destroy():
        pending.clear()  # the process's in-memory unsigned queue dies with it
        close()

    channel.close = destroy
    if cancel_failure == "timeout":

        class LiveProcess:
            killed = False

            def poll(self):
                return 0 if self.killed else None

            def kill(self):
                self.killed = True

        channel.proc = LiveProcess()  # timeout leaves a live signer, even with an unsettled sign

    def delayed(op, args):
        if op == "sign":
            pending.add(args["request_id"])
            raise ClickError("internal", "lost held reply")
        if op == "cancel_holds":
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            if cancel_failure == "timeout":
                raise TimeoutError("cancel timed out")
            if cancel_failure:
                raise ClickError("internal", "cancel failed")
            pending.clear()
            return {"state": "cancelled"}
        return original(op, args)

    channel.handler = delayed
    result = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
    )
    assert result.status_code != 200
    assert pending == {REQ}
    # Lost replies must retain the hold routing, including its session check.
    other = other_session(client, auth)
    assert client.post(url(stub, "cancel"), json={"request_id": REQ}, headers=hdr(other)).status_code == 404
    epoch = gateway.click.restarts
    auth.logout(token)
    assert not pending  # advancing the deadline can no longer sign anything
    assert gateway.click.restarts == epoch + int(bool(cancel_failure))
    assert channel.closed == bool(cancel_failure)
    if cancel_failure == "timeout":
        assert channel.proc.killed


def test_d5_hung_cancel_does_not_hold_auth_lock(client, token, stub, script, auth, gateway):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    other = other_session(client, auth)
    channel = script.clients[0]
    original = channel.handler
    entered, release, validated = threading.Event(), threading.Event(), threading.Event()

    def hung(op, args):
        if op == "cancel_holds":
            entered.set()
            assert release.wait(3)
            raise TimeoutError("IPC deadline")
        return original(op, args)

    channel.handler = hung
    logout = threading.Thread(target=auth.logout, args=(token,))
    logout.start()
    try:
        assert entered.wait(2)
        assert token in auth.ending

        def validate():
            assert auth.valid(other)
            validated.set()

        reader = threading.Thread(target=validate)
        reader.start()
        assert validated.wait(1)  # completes even BEFORE the blocked IPC times out
    finally:
        release.set()
        logout.join(3)
    reader.join(2)
    assert not logout.is_alive()
    assert channel.closed
    assert gateway.click.restarts >= 1


def test_d5_logout_waits_for_inflight_hold_outside_auth_lock(client, token, stub, script, auth):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    other = other_session(client, auth)
    channel = script.clients[0]
    original = channel.handler
    sending, release, ending = threading.Event(), threading.Event(), threading.Event()
    pending = set()
    cleanup = auth.on_session_end

    def end(value):
        ending.set()
        cleanup(value)

    auth.on_session_end = end

    def delayed(op, args):
        if op == "sign":
            sending.set()
            assert release.wait(3)
            pending.add(args["request_id"])
            return {"state": "held", "deadline_ms": 10000}
        if op == "cancel_holds":
            pending.clear()
            return {"state": "cancelled"}
        return original(op, args)

    channel.handler = delayed
    results = []
    sender = threading.Thread(
        target=lambda: results.append(
            client.post(
                url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
            )
        )
    )
    sender.start()
    logout = threading.Thread(target=auth.logout, args=(token,))
    try:
        assert sending.wait(2)
        logout.start()
        assert ending.wait(2)
        assert token in auth.ending
        assert auth.valid(other)
        assert not auth.valid(token)
    finally:
        release.set()
        sender.join(3)
        logout.join(3)
    assert not sender.is_alive() and not logout.is_alive()
    assert results[0].status_code == 200
    assert not pending


@pytest.mark.parametrize("header", [None, "3", "4"])
def test_every_click_route_requires_current_page_contract(client, token, stub, header):
    from fastapi.routing import APIRoute

    client.headers.pop("X-Orch-Click-Contract", None)
    headers = hdr(token)
    if header is not None:
        headers["X-Orch-Click-Contract"] = header
    routes = [r for r in client.app.routes if isinstance(r, APIRoute) and "/click/" in r.path]
    assert len(routes) == 17
    for route in routes:
        path = route.path.replace("{ws}", stub.ws)
        res = client.request(next(iter(route.methods)), path, json={}, headers=headers)
        error = res.json().get("error", {})
        if header != "4":
            assert error.get("code") == "click.contract", (path, res.text)
            assert error["message"] == "Reload the page: this page is out of date."
        else:
            assert error.get("message") != "Reload the page: this page is out of date.", path


@pytest.mark.parametrize("op,risk", [("approve", ["risk"]), ("member.role", [])])
def test_relay_refuses_risk_allowlist_mismatch(op, risk):
    from orch_dashboard.click import contract4_view_ok

    assert not contract4_view_ok({"op": op, "headline": "Review", "risk_lines": risk})


def test_relay_caps_headline_at_sixty():
    from orch_dashboard.click import contract4_view_ok

    assert contract4_view_ok({"op": "approve", "headline": "x" * 60, "risk_lines": []})
    assert not contract4_view_ok({"op": "approve", "headline": "x" * 61, "risk_lines": []})


def test_d5_logout_everywhere_continues_after_callback_failure(client, token, stub, script, auth):
    other = other_session(client, auth)
    prepare(client, other, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    original = script.clients[0].handler
    pending = set()

    def delayed(op, args):
        if op == "sign":
            pending.add(args["session"])
            return {"state": "held", "deadline_ms": 10000}
        if op == "cancel_holds":
            pending.discard(args["session"])
            return {"state": "cancelled"}
        return original(op, args)

    script.clients[0].handler = delayed
    result = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(other)
    )
    assert result.status_code == 200
    cleanup = auth.on_session_end
    seen = []

    def end(value):
        seen.append(value)
        if value == token:
            raise RuntimeError("first cleanup failed")
        cleanup(value)

    auth.on_session_end = end
    auth.logout_everywhere(kind=auth.login_kind(token))
    assert seen == [token, other]
    assert not pending
    assert not auth.ending


def test_d5_background_renews_only_live_holds(client, token, stub, script, auth):
    prepare(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hello"})
    original = script.clients[0].handler
    renewed = threading.Event()
    finished = threading.Event()

    def delayed(op, args):
        if op == "sign" or (op == "status" and args.get("request_id")):
            return {"state": "submitted" if finished.is_set() else "held", "deadline_ms": 10000}
        if op == "session_alive":
            assert args["session"] == hashlib.sha256(token.encode()).hexdigest()
            renewed.set()
            return {"state": "alive"}
        return original(op, args)

    script.clients[0].handler = delayed
    result = client.post(
        url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH, "delay_s": 10}, headers=hdr(token)
    )
    assert result.status_code == 200
    assert renewed.wait(3)
    finished.set()
    renewed.clear()
    assert not renewed.wait(2.5)
    auth.logout(token)


# -- R2 T4: a remote-access change is viewed and signed only by the loopback session that prepared it ---------------

REMOTE_SET = {"action": "remote_set", "level": "view", "premises": "no", "days": 1}


def view_of(client, token, stub):
    return client.post(url(stub, "view"), json={"request_id": REQ}, headers=hdr(token))


def test_a_remote_session_cannot_even_prepare_a_remote_change(client, token, stub, backup, monkeypatch):
    monkeypatch.setattr(client_login_kind(), "login_kind", lambda self, t: "remote", raising=False)
    res = prepare(client, token, stub, REMOTE_SET)
    assert res.status_code == 403 and res.json()["error"]["code"] == "op.not_allowed"


STATES = (
    "prepared",
    "viewed",
    "signed",
    "cancelled",
    "forgotten",
    "expired",
    "record_dropped",
    "record_replaced",
    "unknown",
)
TOKENS = ("same", "other_loopback", "remote", "none")
STEPS = ("view", "sign")


def put_in_state(client, token, stub, backup, state):
    """A remote change prepared by `token`, then brought into `state` the way the code does it."""
    clicks = client.app.state.clicks
    if state == "unknown":
        return
    prepared_ok = prepare(client, token, stub, REMOTE_SET).status_code == 200
    assert prepared_ok
    backup.view = backup_view(op="remote.set")
    if state == "viewed":
        assert view_of(client, token, stub).status_code == 200
    elif state in ("signed", "cancelled", "forgotten"):
        clicks.forget(REQ)  # every end of a request goes through forget: it tombstones the binding
    elif state == "expired":
        clicks.forget(REQ)
        clicks.clock = lambda t=clicks.clock(): t + 10_000  # past BINDING_KEEP: purged (and the record is long gone)
    elif state == "record_dropped":
        clicks.ops.pop(REQ, None)
    elif state == "record_replaced":
        ws, _, _, epoch = clicks.ops[REQ]
        clicks.ops[REQ] = (ws, "approve", None, epoch)


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("who", TOKENS)
@pytest.mark.parametrize("state", STATES)
def test_authorize_table_over_states_tokens_and_steps(client, token, stub, backup, auth, monkeypatch, state, who, step):
    put_in_state(client, token, stub, backup, state)
    other = other_session(client, auth)
    caller = {"same": token, "other_loopback": other, "remote": other, "none": None}[who]
    if who == "remote":
        real = client_login_kind().login_kind
        monkeypatch.setattr(
            client_login_kind(), "login_kind", lambda self, t: "remote" if t == other else real(self, t), raising=False
        )
    headers = hdr(caller) if caller else {"Origin": ORIGIN}
    if step == "view":
        res = client.post(url(stub, "view"), json={"request_id": REQ}, headers=headers)
    else:
        res = client.post(
            url(stub, "sign"),
            json={"request_id": REQ, "view_digest": HASH, "sealed": {"hub.backup_key": BACKUP_ENV}},
            headers=headers,
        )
    open_gate = who == "same" and state in ("prepared", "viewed", "record_dropped", "record_replaced")
    if not open_gate:
        assert res.status_code in (401, 403, 404), (state, who, step, res.status_code)
        if who != "none":
            assert res.status_code == 404, (state, who, step)
        assert sent_of(backup, "sign") == []  # nothing reached the signer
    else:
        blind = step == "sign" and state in ("record_dropped", "record_replaced")  # never signed without the action
        assert res.status_code != 404 or blind, (state, who, step)


def test_a_bound_request_cannot_be_taken_over_by_preparing_it_again_from_another_session(
    client, token, stub, backup, auth
):
    assert prepare(client, token, stub, REMOTE_SET).status_code == 200
    other = other_session(client, auth)
    res = prepare(client, other, stub, REMOTE_SET)
    assert res.status_code in (404, 409), res.text
    assert client.app.state.clicks.binding(REQ)[0] == token


def test_a_tombstone_is_kept_for_its_lifetime_and_purged_after(client, token, stub, backup):
    assert prepare(client, token, stub, REMOTE_SET).status_code == 200
    clicks = client.app.state.clicks
    clicks.forget(REQ)
    assert clicks.binding(REQ) == (token, True)  # tombstoned, not deleted
    start = clicks.clock()
    clicks.clock = lambda: start + 1
    assert clicks.binding(REQ) is not None
    clicks.clock = lambda: start + 10_000
    assert clicks.binding(REQ) is None
