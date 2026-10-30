"""Every answer that maps to `internal` leaves the exception's type and frames in the log, never a message or value
(as core's transport `_log_internal`, #1659): a 500 once left no trace of its cause (approve_both, run 12)."""

import logging

from conftest import ORIGIN, PORT  # noqa: F401

from orch_dashboard.errors import HostError

SECRET = "sk-live-SECRET-VALUE-123"


def _get(client, token, path):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_an_unexpected_exception_logs_its_type_and_frames_but_no_value(client, token, gateway, monkeypatch, caplog):
    def boom():
        raise ValueError(SECRET)

    monkeypatch.setattr(gateway, "workspaces", boom)
    with caplog.at_level(logging.ERROR, logger="orch_dashboard"):
        res = _get(client, token, "/api/workspaces")
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"
    text = caplog.text
    assert "ValueError" in text and "boom" in text and "test_internal_log.py:" in text
    assert SECRET not in text and SECRET not in res.text


def test_a_host_error_that_maps_to_internal_is_logged_the_same_way(client, token, gateway, monkeypatch, caplog):
    def bad():
        raise HostError({"code": "internal", "message": SECRET}, exit=1)

    monkeypatch.setattr(gateway, "workspaces", bad)
    with caplog.at_level(logging.ERROR, logger="orch_dashboard"):
        res = _get(client, token, "/api/workspaces")
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"
    assert "HostError" in caplog.text and "bad" in caplog.text
    assert SECRET not in caplog.text
