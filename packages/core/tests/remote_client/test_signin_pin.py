"""R5 T1: sign-in only over pinned TLS (no plain http off loopback, no trust on first use)."""

from __future__ import annotations

import pytest

from orch.hubclient.netconfig import NetSettings
from orch.remoteclient import Client, ClientError, Store
from orch.remoteclient.codes import plain

PIN = "A" * 43


def client(tmp_path, tls_result=b"", http_result=(500, b"")):
    calls = {"http": [], "tls": []}

    def http(m, u, b, h):
        calls["http"].append((m, u))
        return http_result

    def tls(host, port, pin, req):
        calls["tls"].append((host, port, pin))
        if isinstance(tls_result, Exception):
            raise tls_result
        return tls_result

    return Client(Store(tmp_path / "rc"), http=http, tls_request=tls, net=NetSettings()), calls


@pytest.mark.parametrize("origin", ["http://hub.office:8443", "http://10.0.0.5:8080", "http://localhost:9"])
def test_plain_http_off_loopback_refuses_before_any_connection(tmp_path, origin):
    c, calls = client(tmp_path)
    with pytest.raises(ClientError) as e:
        c.connect_hub(origin, "hub.office:9443", "ann", "pw", "123456", pin=PIN)
    assert e.value.code == "remote_client.no_pin" and calls == {"http": [], "tls": []}


def test_no_pin_known_refuses_with_the_plain_message_sending_nothing(tmp_path):
    c, calls = client(tmp_path)
    with pytest.raises(ClientError) as e:
        c.connect_hub("https://hub.office:8443", "hub.office:9443", "ann", "pw", "123456")
    assert e.value.message == "Use the connect link from your hub admin." == plain("remote_client.no_pin")
    assert calls == {"http": [], "tls": []}


def test_malformed_pin_refuses(tmp_path):
    c, calls = client(tmp_path)
    with pytest.raises(ClientError):
        c.connect_hub("https://hub.office:8443", "hub.office:9443", "ann", "pw", "1", pin="short")
    assert calls["tls"] == []


def test_pin_mismatch_refuses(tmp_path):
    c, calls = client(tmp_path, tls_result=ClientError("remote_client.hub_key_changed"))
    with pytest.raises(ClientError) as e:
        c.connect_hub("https://hub.office:8443", "hub.office:9443", "ann", "pw", "1", pin=PIN)
    assert e.value.code == "remote_client.hub_key_changed"
    assert calls["tls"] == [("hub.office", 8443, PIN)]  # only the card request was tried: no password sent


def test_loopback_http_goes_through_the_plain_path(tmp_path):
    c, calls = client(tmp_path, http_result=(404, b""))
    for origin in ("http://127.0.0.1:8443", "http://[::1]:8443"):
        with pytest.raises(ClientError) as e:
            c.connect_hub(origin, "127.0.0.1:9443", "ann", "pw", "1")
        assert e.value.code == "remote_client.no_hub"
    assert len(calls["http"]) == 2 and calls["tls"] == []


def test_loopback_hub_with_a_pin_uses_pinned_tls_never_plain_http(tmp_path):
    c, calls = client(tmp_path, tls_result=ClientError("remote_client.hub_key_changed"))
    with pytest.raises(ClientError) as e:
        c.connect_hub("https://127.0.0.1:8443", "127.0.0.1:9443", "ann", "pw", "1", pin=PIN)
    assert e.value.code == "remote_client.hub_key_changed"
    assert calls == {"http": [], "tls": [("127.0.0.1", 8443, PIN)]}


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8443", "http://[::1]:8443"])
def test_loopback_http_with_a_pin_is_refused_not_sent_in_clear(tmp_path, origin):
    c, calls = client(tmp_path)
    with pytest.raises(ClientError) as e:
        c.connect_hub(origin, "127.0.0.1:9443", "ann", "pw", "1", pin=PIN)
    assert e.value.code == "remote_client.no_pin" and calls == {"http": [], "tls": []}
