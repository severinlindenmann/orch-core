"""Review I4: the top level of the browser's error is an allow-list (`errors.TOP_KEYS`). Every allowed key passes with
a valid value; a value of the wrong type or shape and every unknown key are dropped; a missing or invalid `code`
answers the dashboard's own `internal`."""

import json

import pytest

from orch_dashboard import errors
from orch_dashboard.errors import HostError, host_response

RID = "hr_01JABCDEFGHJKMNPQRSTVWXYZ0"
SHA1 = "a" * 40
SHA256 = "b" * 64


def body(error: dict, exit: int = 3):
    res = host_response(HostError(error, exit))
    return res.status_code, json.loads(res.body)


def base(code="human.click_refused", **extra):
    return {"code": code, "message": "m", "retryable": False, **extra}


# ---- every allowed key passes with a valid value ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,key,value",
    [
        ("host.restarting", "hint", "wait a moment"),
        ("host.restarting", "retryable", True),
        ("rate.limited", "retry_after_ms", 0),
        ("rate.limited", "retry_after_ms", 3_600_000),
        ("human.click_refused", "request_id", RID),
        ("human.click_refused", "request_id", RID[3:]),
        ("intent.done", "signed_at", "2026-10-15T12:00:00Z"),
        ("intent.done", "signed_at", "2026-10-15T12:00:00.123+02:00"),
        ("intent.done", "signed_at", None),
        ("intent.done", "op", "gate.approve"),
        ("intent.done", "summary", '[{"type": "gate.approved"}]'),
        ("intent.done", "summary", ""),
        ("records.push_failed", "commit", SHA1),
        ("records.push_failed", "commit", SHA256),
        ("records.push_failed", "message_hidden", True),
        ("intent.failed", "reason", "human.request_state"),
        ("intent.ended", "state", "cancelled"),
        ("intent.ended", "state", "expired"),
        ("intent.done", "state", "submitted"),
        ("intent.failed", "state", "submitted"),
    ]
    + [("human.click_refused", "reason", r) for r in errors.CLICK_REFUSALS]
    + [("sign.convenience_unavailable", "reason", r) for r in errors.REASONS["sign.convenience_unavailable"]]
    + [("sign.convenience_readers", "reason", r) for r in errors.REASONS["sign.convenience_readers"]],
)
def test_every_allowed_key_passes_with_a_valid_value(code, key, value):
    _, got = body(base(code, **{key: value}))
    assert got["error"][key] == value


def test_the_whole_allowed_shape_passes_unchanged():
    error = {
        "code": "intent.done",
        "message": "This is already done.",
        "hint": "h",
        "retryable": False,
        "retry_after_ms": 5,
        "state": "submitted",
        "signed_at": "2026-10-15T12:00:00Z",
        "op": "answer",
        "summary": "[]",
        "message_hidden": False,
    }
    status, got = body(error, 5)
    assert status == 409 and got == {"ok": False, "exit": 5, "error": error}


# ---- wrong types and shapes are dropped ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,key,value",
    [
        ("rate.limited", "retry_after_ms", True),  # a bool for an int
        ("rate.limited", "retry_after_ms", -1),
        ("rate.limited", "retry_after_ms", 3_600_001),
        ("rate.limited", "retry_after_ms", 1.5),
        ("rate.limited", "retry_after_ms", "10"),
        ("host.restarting", "hint", {"a": 1}),  # a dict for a str
        ("host.restarting", "hint", "x" * 401),  # over-long
        ("host.restarting", "hint", ""),
        ("records.push_failed", "message_hidden", "yes"),
        ("records.push_failed", "message_hidden", 1),
        ("human.click_refused", "why", "/Users/me/secret"),
        ("human.click_refused", "why", True),
        ("human.click_refused", "request_id", "../../etc"),
        ("human.click_refused", "request_id", RID.lower()),
        ("intent.done", "signed_at", "yesterday"),
        ("intent.done", "signed_at", 1760529600),
        ("intent.done", "op", "Gate Approve"),
        ("intent.done", "op", ["answer"]),
        ("intent.done", "summary", "x" * (errors.SUMMARY_MAX + 1)),
        ("intent.done", "summary", {"review": []}),
        ("records.push_failed", "commit", "a" * 39),
        ("records.push_failed", "commit", "A" * 40),
        ("records.push_failed", "commit", "ghp_" + "a" * 36),
        # reason: only from the closed set of its code
        ("human.click_refused", "reason", "the token was ghp_abc"),
        ("human.click_refused", "reason", "relay_paired"),  # a reason of another code
        ("sign.convenience_readers", "reason", "disabled"),
        ("host.unavailable", "reason", "terminal_only"),  # a code without a reason set
        ("intent.failed", "reason", "Not A Code"),
        ("intent.failed", "reason", ["x"]),
        ("intent.ended", "state", "submitted"),  # state: only the closed set of its code
        ("intent.done", "state", "/tmp/x"),
        ("host.unavailable", "state", "submitted"),
    ],
)
def test_a_value_of_the_wrong_type_or_shape_is_dropped(code, key, value):
    _, got = body(base(code, **{key: value}))
    assert key not in got["error"]


def test_a_bad_retryable_reads_as_not_retryable_and_a_bad_message_as_the_code():
    _, got = body({"code": "host.busy", "message": {"path": "/x"}, "retryable": "yes"})
    assert got["error"] == {"code": "host.busy", "message": "host.busy", "retryable": False}
    _, got = body({"code": "host.busy", "message": "x" * (errors.MESSAGE_MAX + 1), "retryable": True})
    assert got["error"]["message"] == "host.busy" and got["error"]["retryable"] is True
    _, got = body({"code": "host.busy"})
    assert got["error"] == {"code": "host.busy", "message": "host.busy", "retryable": False}


# ---- unknown keys are dropped ---------------------------------------------------------------------------------------
def test_unknown_keys_are_dropped():
    error = base(
        "host.unavailable",
        path="/Users/me/.config/orch/keys",
        token="ghp_" + "a" * 36,
        debug={"stack": ["frame"], "env": {"HOME": "/Users/me"}},
        state="submitted",
        exit=4,
        ok=True,
    )
    _, got = body(error)
    assert got["error"] == {"code": "host.unavailable", "message": "m", "retryable": False}


# ---- an invalid or missing code gives internal ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        {"message": "m", "retryable": False, "path": "/x"},
        {"code": None, "message": "m"},
        {"code": 7, "message": "m"},
        {"code": "Not.A.Code", "message": "m"},
        {"code": "/Users/me/secret", "message": "m"},
        {"code": "a." + "b" * 70, "message": "m"},
        {"code": "", "message": "m"},
        {"code": {"nested": "x"}, "message": "m"},
    ],
)
def test_an_invalid_or_missing_code_answers_internal(error):
    status, got = body(error, 3)
    assert status == 500
    assert got == {"ok": False, "exit": 1, "error": {"code": "internal", "message": "internal", "retryable": False}}


def test_a_non_dict_host_error_answers_internal():
    status, got = body("boom")  # type: ignore[arg-type]
    assert status == 500 and got["error"]["code"] == "internal"


# ---- the server's own errors keep what the page reads ---------------------------------------------------------------
def test_the_servers_own_errors_pass_whole():
    from orch_dashboard.click import refusal_check
    from orch_dashboard.click_signer import lost_sign
    from orch_dashboard.errors import fail
    from orch_dashboard.handoff import done_error

    assert body(refusal_check("x").error)[1]["error"]["reason"] == "check_failed"
    assert body(lost_sign().error)[1]["error"]["reason"] == "uncertain"
    done = done_error(
        {"request": {"submitted_at": "2026-10-15T12:00:00Z"}, "op": "answer", "material": {"review": ["a"]}},
        {"seq": 3},
    )
    got = body(done.error, 5)[1]["error"]
    assert (got["signed_at"], got["op"], got["summary"]) == ("2026-10-15T12:00:00Z", "answer", '["a"]')
    failed = body(done_error({}, {"seq": None, "error": {"code": "human.request_state"}}).error, 5)[1]["error"]
    assert failed["reason"] == "human.request_state"
    assert body(fail("lock.busy").error, 9)[1]["error"]["retryable"] is True


def test_the_click_refusals_match_cores():
    """`CLICK_REFUSALS` mirrors core's `orch.click.protocol.REFUSALS`, plus `disabled` (the page reads it)."""
    from orch.click.protocol import REFUSALS

    assert set(errors.CLICK_REFUSALS) == set(REFUSALS) | {"disabled"}


def test_the_code_pattern_admits_every_code_core_and_the_dashboard_send():
    from orch.host.errors import HOST_CODES
    from orch.ops.errors import ERRORS

    for code in [*HOST_CODES, *ERRORS, "intent.done", "intent.failed", "records.child_invalid", "human.click_refused"]:
        assert errors.CODE.fullmatch(code) and len(code) <= errors.CODE_MAX, code


# ---- review I4 follow-up (Opus CHANGES) -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exit", [{"path": "/Users/me/.config/orch/key", "token": "ghp_x"}, "ghp_secret", True, None, 0, 10, -1, 3.0]
)
def test_m1_an_invalid_exit_never_reaches_the_page(exit):
    """M1: the response's `exit` is a plain int 1..9 (§0.6), else the dashboard's 1."""
    _, got = body(base("host.busy"), exit)
    assert got["exit"] == 1 and type(got["exit"]) is int
    exc = HostError(base("host.busy"), 3)
    exc.exit = exit  # a later assignment is checked again at the response
    again = json.loads(host_response(exc).body)["exit"]
    assert again == 1 and type(again) is int


@pytest.mark.parametrize("exit", [1, 5, 9])
def test_m1_a_valid_exit_passes(exit):
    assert body(base("host.busy"), exit)[1]["exit"] == exit


@pytest.mark.parametrize(
    "frame",
    [
        {"error": {"code": "host.busy", "message": "m", "retryable": True}, "exit": {"path": "/x", "token": "t"}},
        {"error": {"code": "host.busy", "message": "m", "retryable": True}},  # no exit at all
        {"error": {"message": "no code", "path": "/x"}, "exit": 3},  # L1: a missing code
        {"error": {"code": 7, "message": "m"}, "exit": 3},  # L1: a non-string code
        {"exit": 3},  # no error at all
    ],
)
def test_m1_l1_hostlink_reads_a_bad_error_frame_as_a_checked_error(frame):
    import os
    import socket
    import threading

    from test_hostlink import WS, welcome

    from orch_dashboard.hostlink import HostLink, encode, receive

    a, b = socket.socketpair()

    def host():
        with b:
            receive(b)
            b.sendall(encode(welcome()))
            req = receive(b)
            b.sendall(encode({"t": "res", "id": req["id"], "ok": False, **frame}))

    t = threading.Thread(target=host)
    t.start()
    link = HostLink(a, os.getuid(), WS)
    with pytest.raises(HostError) as err:
        link.call("whoami")
    link.close()
    t.join()
    got = json.loads(host_response(err.value).body)
    assert 1 <= got["exit"] <= 9 and isinstance(got["exit"], int)
    code = frame.get("error", {}).get("code")
    assert got["error"]["code"] == (code if code == "host.busy" else "internal")
    assert "path" not in json.dumps(got) and "token" not in json.dumps(got)


@pytest.mark.parametrize("error", [None, "boom", {"message": "m"}, {"code": "/Users/me/x"}, {"code": ["a"]}])
def test_l1_l2_every_host_error_holds_a_checked_code(error):
    """L1/L2: `exc.error["code"]` is valid wherever it is read (hostlink, gateway host_state, live, terminals)."""
    exc = HostError(error, 3)  # type: ignore[arg-type]
    assert exc.error == {"code": "internal", "message": "internal", "retryable": False} and exc.exit == 1


def test_l2_the_terminal_end_frame_checks_code_and_retry_after():
    from orch_dashboard import terminals

    exc = HostError(base("rate.limited", retryable=True), 9)
    exc.error["code"] = "ghp_" + "a" * 36 + "/x"  # a code changed after construction is checked again
    assert terminals.host_closed(exc).code == "internal"
    for bad in (-5, 10**12, True):
        closed = terminals.host_closed(HostError(base("rate.limited", retryable=True, retry_after_ms=bad), 9))
        assert closed.end()["retry_after_ms"] == 1000, bad
    closed = terminals.host_closed(HostError(base("rate.limited", retryable=True, retry_after_ms=700), 9))
    assert closed.end()["retry_after_ms"] == 700


@pytest.mark.parametrize(
    "code,key,value",
    [
        ("host.busy", "summary", "A similar thing was signed"),
        ("intent.ended", "summary", "[]"),
        ("host.busy", "signed_at", "2026-10-15T12:00:00Z"),
        ("host.busy", "op", "answer"),
        ("host.busy", "request_id", RID),
        ("host.busy", "why", "update_orch"),
    ],
)
def test_l3_the_servers_own_keys_only_on_the_codes_that_own_them(code, key, value):
    _, got = body(base(code, **{key: value}))
    assert key not in got["error"]


def test_i1_a_long_review_summary_still_passes():
    summary = json.dumps([{"body": "x" * 30_000}])
    assert body(base("intent.done", summary=summary))[1]["error"]["summary"] == summary


def test_i2_a_plain_text_fix_is_capped():
    assert body(base("host.busy", fix="update the readers first"))[1]["error"]["fix"] == "update the readers first"
    assert "fix" not in body(base("host.busy", fix="x" * (errors.FIX_TEXT_MAX + 1)))[1]["error"]
    assert "fix" not in body(base("host.busy", fix=""))[1]["error"]


@pytest.mark.parametrize("code", ["records.push_failed", "host.busy", "lock.busy", "store.busy"])
def test_a_landed_commit_rides_on_any_push_failure(code):
    """Core attaches the landed local commit to every push failure, a host refusal included (review I4 delta N1): the
    Records page then says only the push failed."""
    _, got = body(base(code, commit=SHA1))
    assert got["error"]["commit"] == SHA1
    _, bad = body(base(code, commit="not-a-sha"))
    assert "commit" not in bad["error"]
