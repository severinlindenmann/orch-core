"""Phone hints are a person's unsigned requests, never agent writes."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orch_dashboard.errors import HostError
from orch_dashboard.phone import person_link, register

WS = "a" * 32
ID = "ph_" + "0" * 25 + "1"


@pytest.mark.parametrize(
    "peer",
    [
        {"class": "agent", "person": "p_a"},
        {"class": "person"},
        {"class": "person", "person": "p_a", "grant": "g_a"},
        {"class": "person", "person": "p_a", "session": "s_a"},
    ],
)
def test_non_person_never_sends_phone_op(peer, monkeypatch):
    monkeypatch.delenv("ORCH_GRANT", raising=False)
    monkeypatch.delenv("ORCH_SESSION", raising=False)
    link = Mock(welcome={"peer": peer})
    with pytest.raises(HostError):
        person_link(SimpleNamespace(enabled=lambda ws: link), WS)
    link.call.assert_not_called()


@pytest.mark.parametrize("env", ["ORCH_GRANT", "ORCH_SESSION"])
def test_agent_environment_is_refused_before_connecting(monkeypatch, env):
    monkeypatch.setenv(env, "demo-not-a-real-context")
    gateway = Mock()
    with pytest.raises(HostError):
        person_link(gateway, WS)
    gateway.enabled.assert_not_called()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("ORCH_GRANT", raising=False)
    monkeypatch.delenv("ORCH_SESSION", raising=False)
    link = Mock(welcome={"peer": {"class": "person", "person": "p_a"}})
    link.call.return_value = {"id": ID, "state": "open"}
    app = FastAPI()
    register(app, SimpleNamespace(enabled=lambda ws: link))
    return TestClient(app), link


def test_request_passes_exact_gate_generation_and_idem(setup):
    client, link = setup
    body = {"ref": "DEMO-1", "gate": "plan", "gate_gen": 7, "idem": "attempt-1"}
    assert client.post(f"/api/w/{WS}/phone/request", json=body).status_code == 200
    link.call.assert_called_once_with(
        "phone.request", {"ref": "DEMO-1", "gate": "plan", "gate_gen": 7}, write=True, idem="attempt-1"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"gate": "code"},
        {"gate": "verify"},
        {"gate_gen": True},
        {"text": "changes"},
        {"auth": {"grant": "x"}},
        {"gate_gen": -1},
    ],
)
def test_invalid_or_phase_two_requests_never_reach_host(setup, patch):
    client, link = setup
    body = {"ref": "DEMO-1", "gate": "requirements", "gate_gen": 0, "idem": "attempt-1", **patch}
    assert client.post(f"/api/w/{WS}/phone/request", json=body).status_code == 422
    link.call.assert_not_called()


def test_cancel_and_status_fixed_ops(setup):
    client, link = setup
    assert client.post(f"/api/w/{WS}/phone/cancel", json={"id": ID}).status_code == 200
    link.call.assert_called_with("phone.cancel", {"id": ID}, write=True, idem="cancel-" + ID)
    assert client.get(f"/api/w/{WS}/phone/status/{ID}").status_code == 200
    link.call.assert_called_with("phone.status", {"id": ID})


def test_only_current_persons_valid_phone_profile_counts(setup):
    client, link = setup
    link.call.return_value = {
        "members": [
            {
                "person": "p_a",
                "devices": [
                    {"valid": True, "scopes": ["look", "decide"]},
                    {"valid": False, "scopes": ["decide"]},
                    {"valid": True, "scopes": ["decide", "write"]},
                ],
            },
            {"person": "p_b", "devices": [{"valid": True, "scopes": ["decide"]}]},
        ]
    }
    assert client.get(f"/api/w/{WS}/phone/available").json() == {"phones": 1}
