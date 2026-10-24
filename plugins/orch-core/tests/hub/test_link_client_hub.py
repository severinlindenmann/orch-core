"""C5 cross-repo: the link client against orch-hub's real link server (D5), run in a subprocess under the hub's own
environment. Skipped unless ``ORCH_HUB_SRC`` points at an orch-hub checkout with a usable ``uv`` project (like D11's
``ORCH_PUBLISH_SRC``). Core never imports hub code; the server only runs as a child process."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch.crypto import suite
from orch.crypto.encoding import unb64u
from orch.hubclient.tlslink.client import LinkClient, LinkError

HUB_SRC = os.environ.get("ORCH_HUB_SRC", "")
pytestmark = pytest.mark.skipif(
    not (HUB_SRC and (Path(HUB_SRC) / "src" / "orch_hub" / "link").is_dir() and shutil.which("uv")),
    reason="ORCH_HUB_SRC not set to an orch-hub checkout with the link server",
)


@pytest.fixture
def hub() -> Iterator[dict]:
    p = subprocess.Popen(
        ["uv", "run", "--project", HUB_SRC, "python", str(Path(__file__).with_name("hub_server_proc.py")), HUB_SRC],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = p.stdout.readline()
        if not line:
            pytest.skip("hub server did not start")
        yield json.loads(line)
    finally:
        p.stdin.close()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_real_hub_join_then_snapshot(hub: dict) -> None:
    wsk = suite.private_key_from_scalar(hub["wsk_scalar"])
    wsk_pub = suite.public_bytes(wsk)

    def sign(msg: bytes) -> bytes:
        return suite.sign(wsk, msg)

    def link(**kw) -> LinkClient:
        return LinkClient.open(
            "127.0.0.1",
            hub["port"],
            tls_spki_sha256=hub["spki"],
            hub_id=hub["hub_id"],
            workspace_id=hub["workspace_id"],
            sign=sign,
            hk_op_pub=unb64u(hub["hk_op_pub"], 65),
            timeout=10,
            **kw,
        )

    with link(wsk_pub=wsk_pub) as c:  # first link of a join
        assert c.request("join", hub["join"], sign=sign, seq=1) == {}
        snap = c.request("snapshot_get", {}, sign=sign, seq=2)["snapshot"]
        assert snap["o"]["hub_id"] == hub["hub_id"] and snap["o"]["op_cert_id"] == hub["op_cert_id"]
    with link() as c:  # later links: the hub holds the pin
        assert c.request("snapshot_get", {}, sign=sign, seq=1)["snapshot"]["o"]["hub_id"] == hub["hub_id"]
    other = suite.generate_private_key()
    with pytest.raises(LinkError) as e:
        LinkClient.open(
            "127.0.0.1",
            hub["port"],
            tls_spki_sha256=hub["spki"],
            hub_id=hub["hub_id"],
            workspace_id=hub["workspace_id"],
            sign=lambda m: suite.sign(other, m),
            hk_op_pub=unb64u(hub["hk_op_pub"], 65),
            timeout=10,
        )
    assert e.value.code == "auth_failed"


def test_real_hub_refuses_a_join_without_the_owner_proof_and_with_a_forged_one(hub: dict) -> None:
    """#1198 C9, protocol 9.6 "At the hub": the real hub wants the owner's proof whatever the host sends."""
    wsk = suite.private_key_from_scalar(hub["wsk_scalar"])

    def sign(msg: bytes) -> bytes:
        return suite.sign(wsk, msg)

    def first_link() -> LinkClient:
        return LinkClient.open(
            "127.0.0.1", hub["port"], tls_spki_sha256=hub["spki"], hub_id=hub["hub_id"],
            workspace_id=hub["workspace_id"], sign=sign, hk_op_pub=unb64u(hub["hk_op_pub"], 65), timeout=10,
            wsk_pub=suite.public_bytes(wsk),
        )  # fmt: skip

    forged = dict(hub["join"]["owner_proof"], sig=hub["join"]["owner_proof"]["sig"][:-2] + "AA")
    for proof, code in ((None, "proof_missing"), (forged, "proof_invalid")):
        with first_link() as c, pytest.raises(LinkError) as e:
            c.request("join", {**hub["join"], "owner_proof": proof}, sign=sign, seq=1)
        assert e.value.code == code
    with first_link() as c:  # nothing was pinned or consumed: the real join still works
        assert c.request("join", hub["join"], sign=sign, seq=1) == {}
