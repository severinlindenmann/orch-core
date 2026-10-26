"""Plain http is accepted for loopback, private IPv4 and CGNAT (100.64/10) hosts, https for any host."""

import pytest

from orch_hub.contract import hub
from orch_hub.publish.forwarder import _endpoint

OK = [
    "http://127.0.0.1:8100",
    "http://10.1.2.3",
    "http://172.16.0.1:8101",
    "http://192.168.1.10:80",
    "http://100.71.14.98:8102",
]
BAD = ["http://8.8.8.8", "http://172.32.0.1", "http://100.128.0.1", "http://relay.example.com", "http://10.0.0.256"]


@pytest.mark.parametrize("u", OK)
def test_contract_url_accepts(u):
    assert hub._relay_url(u) and hub._URL.fullmatch(u)
    assert _endpoint(u)[0] == "http"


@pytest.mark.parametrize("u", BAD)
def test_contract_url_refuses(u):
    assert not hub._relay_url(u)
    with pytest.raises(ValueError):
        _endpoint(u)
