"""§0.3 ``Error.detail`` (contract v1.9, #479): a closed per-code schema; anything else is dropped before sending."""

from __future__ import annotations

import pytest

from orch.host.errors import DETAIL_KEYS, HostError
from orch.host.frames import FrameError, validate_envelope

ALLOWED = {
    "term.not_attachable": {"state": "starting"},
    "run.limit": {"which": "agent_ws"},
    "launch.no_grant": {"reason": "expiring"},
    "runner.disabled": {"missing": "machine"},
    "ticket.key_not_next": {"next_key": "ORCH-0042"},
}


def res(error: dict) -> dict:
    return {"t": "res", "id": "r1", "ok": False, "exit": 3, "error": error}


@pytest.mark.parametrize("code", sorted(ALLOWED))
def test_each_allowed_code_sends_exactly_its_allowed_keys(code):
    err = HostError(code, "m", detail=ALLOWED[code]).error()
    assert err["detail"] == ALLOWED[code]
    assert set(err["detail"]) == set(DETAIL_KEYS[code])
    validate_envelope(res(err), sender="host")


def test_the_table_is_exactly_the_five_codes():
    assert set(DETAIL_KEYS) == set(ALLOWED)


@pytest.mark.parametrize("code", sorted(ALLOWED))
def test_an_unlisted_key_never_leaks(code):
    detail = dict(ALLOWED[code], secret="s3cret", note="free text")
    err = HostError(code, "m", detail=detail).error()
    assert err["detail"] == ALLOWED[code]


def test_an_unlisted_code_never_carries_detail():
    err = HostError("not_found", "m", detail={"state": "running", "next_key": "ORCH-0001"}).error()
    assert "detail" not in err


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("term.not_attachable", {"state": "free text with spaces"}),
        ("run.limit", {"which": "everything"}),
        ("launch.no_grant", {"reason": "because"}),
        ("runner.disabled", {"missing": "token"}),
        ("ticket.key_not_next", {"next_key": "not a key"}),
        ("ticket.key_not_next", {"next_key": "ORCH-1\nx"}),
        ("ticket.key_not_next", {"next_key": 5}),
    ],
)
def test_an_open_value_is_dropped_not_sent(code, detail):
    err = HostError(code, "m", detail=detail).error()
    assert "detail" not in err


def test_the_in_process_detail_is_kept_whole():
    e = HostError("ticket.key_not_next", "m", detail={"next_key": "ORCH-0042", "extra": 1})
    assert e.detail == {"next_key": "ORCH-0042", "extra": 1}


def test_stored_round_trips_the_detail():
    err = HostError("run.limit", "m", detail={"which": "runner"}).error()
    assert HostError.stored(err).error() == err


def test_frames_accept_detail_and_reject_a_wrong_type():
    base = {"code": "run.limit", "message": "m", "retryable": True}
    validate_envelope(res(dict(base, detail={"which": "runner"})), sender="host")
    for bad in ("runner", ["x"], {"which": 3}):
        with pytest.raises(FrameError):
            validate_envelope(res(dict(base, detail=bad)), sender="host")
