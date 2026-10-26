"""D12b G6: the host never dials a hub without a real pin (missing, malformed or the card's placeholder)."""

from __future__ import annotations

import base64
import hashlib
import socket

import pytest

from orch.hubclient.tlsadapter import TlsTransport
from orch.hubclient.tlslink.client import LinkClient
from orch.hubclient.tlslink.tls import PLACEHOLDER_PIN, NoPin, PinMismatch, connect_pinned


@pytest.fixture
def listener():
    """A TCP listener that records whether anybody dialled it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    s.settimeout(0.3)
    yield s
    s.close()


def dialled(s: socket.socket) -> bool:
    try:
        s.accept()[0].close()
        return True
    except (TimeoutError, OSError):
        return False


def test_placeholder_matches_the_hub_cards_value():
    digest = hashlib.sha256(b"orch-hub dev: no TLS key pinned yet").digest()
    assert PLACEHOLDER_PIN == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.mark.parametrize("pin", [None, "", "A" * 10, "A" * 44, PLACEHOLDER_PIN, b"x" * 43, 7])
def test_connect_pinned_refuses_before_dialling(listener, pin):
    port = listener.getsockname()[1]
    with pytest.raises(NoPin):
        connect_pinned("127.0.0.1", port, pin, timeout=1)  # type: ignore[arg-type]
    assert not dialled(listener)


def test_nopin_is_a_pinmismatch_so_callers_keep_their_handling():
    assert issubclass(NoPin, PinMismatch)


def test_link_client_refuses_the_placeholder_before_dialling(listener):
    port = listener.getsockname()[1]
    with pytest.raises(NoPin):
        LinkClient.open(
            "127.0.0.1", port, tls_spki_sha256=PLACEHOLDER_PIN, hub_id="0" * 32, workspace_id="1" * 32,
            sign=lambda m: bytes(64), hk_op_pub=bytes(65), timeout=1,
        )  # fmt: skip
    assert not dialled(listener)


@pytest.mark.parametrize("pin", [PLACEHOLDER_PIN, ""])
def test_transport_maps_it_to_unreachable_and_never_dials(listener, pin):
    port = listener.getsockname()[1]
    t = TlsTransport(
        addr=f"127.0.0.1:{port}", tls_spki_sha256=pin, hub_id="0" * 32, workspace_id="1" * 32,
        hk_op_pub=bytes(65), wsk_pub=bytes(65), sign=lambda m: bytes(64), timeout=1,
    )  # fmt: skip
    with pytest.raises(ConnectionError):
        t.send({"o": {"op": "snapshot_get"}, "sig": ""})
    assert not dialled(listener)
