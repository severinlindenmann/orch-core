import pytest


def _link(host):
    return f"orch://hub-pair/{host}#h1.{'a' * 32}.{'b' * 32}.abcd-abcd-abcd"


@pytest.mark.parametrize(
    "host,api",
    [("100.71.14.98:8101", "http://100.71.14.98:8101"), ("10.1.2.3", "http://10.1.2.3"), ("172.20.0.1:80", "http://172.20.0.1:80"),
     ("192.168.1.10:8101", "http://192.168.1.10:8101"), ("hub.example.org", "https://hub.example.org"),
     ("8.8.8.8:8101", "https://8.8.8.8:8101"), ("100.128.0.1", "https://100.128.0.1"), ("172.32.0.1", "https://172.32.0.1")],
)
def test_pair_link_scheme(host, api):
    from orch_hub.contract import channelapi

    assert channelapi.parse_pair_link(_link(host), dev_mode=False)["api"] == api
