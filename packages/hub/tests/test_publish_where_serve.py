"""#1838 step B: the hub signs ``publish_where`` once at start (``pubwhere.prepare``) and serves it in ``hub_where`` and the
``join`` reply; the same bytes after a restart, a higher ``seq`` when an origin changes, nothing (and one line) when the
origins are refused."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys

import pytest
from publish_helpers import HOST, FakeUpstream

from orch_hub import settings as SETT
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.publish_where import check_publish_where
from orch_hub.keys import HubKeys
from orch_hub.link import LinkClient
from orch_hub.pubwhere import PUBLISH_WHERE_FILE, prepare

from publish_helpers import binding  # isort: skip

READY = re.compile(r"orch-hub ready port=(\d+) tls_spki_sha256=([A-Za-z0-9_-]{43}) hub_id=([0-9a-f]{32})\n")
SHARE = "http://127.0.0.1:8104"
PRIVATE = "http://127.0.0.1:8102"
SETTINGS = {
    "v": 1,
    "name": "Hub",
    "company": "",
    "addresses": [{"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"}],
}


def _prep(state, keys, *, api_host=HOST, share=SHARE, private=PRIVATE, dev=True, now=1_790_000_000_000):
    lines: list[str] = []
    got = prepare(
        state,
        keys,
        api_host=api_host,
        share_url=share,
        publish_url=private,
        dev=dev,
        say=lines.append,
        now_ms=lambda: now,
    )
    return got, lines


@pytest.fixture
def keys(tmp_path):
    return HubKeys.init(tmp_path / "hub")


def _check(keys, signed, dev=True):
    return check_publish_where(signed, op_pub=keys.op_signer.public, hub_id=keys.hub_id, dev=dev)


def test_the_object_passes_the_checker_with_the_right_origins(tmp_path, keys):
    got, lines = _prep(tmp_path / "hub", keys)
    assert lines == [] and got is not None
    o = _check(keys, got)
    assert o["server"] == f"http://{HOST}" and o["share"] == SHARE and o["dev"] is True and o["seq"] >= 1
    stored = json.loads((tmp_path / "hub" / PUBLISH_WHERE_FILE).read_text())
    assert stored["signed"] == got and (tmp_path / "hub" / PUBLISH_WHERE_FILE).stat().st_mode & 0o077 == 0


def test_a_restart_serves_identical_bytes_even_later(tmp_path, keys):
    first, _ = _prep(tmp_path / "hub", keys, now=1_790_000_000_000)
    again, _ = _prep(tmp_path / "hub", keys, now=1_790_000_999_000)
    assert first is not None and cj(again) == cj(first)


def test_a_changed_origin_gives_a_higher_seq(tmp_path, keys):
    first, _ = _prep(tmp_path / "hub", keys, now=1_790_000_000_000)
    changed, _ = _prep(
        tmp_path / "hub", keys, share="http://127.0.0.1:8109", now=1_790_000_000_000
    )  # clock stood still
    assert first is not None and changed is not None
    assert changed["o"]["seq"] == first["o"]["seq"] + 1 and changed["o"]["share"] == "http://127.0.0.1:8109"
    server_changed, _ = _prep(tmp_path / "hub", keys, api_host="127.0.0.1:8110", share="http://127.0.0.1:8109")
    assert server_changed is not None and server_changed["o"]["seq"] > changed["o"]["seq"]
    # a later clock wins over stored + 1
    later, _ = _prep(tmp_path / "hub", keys, share=SHARE, now=1_800_000_000_000)
    assert later is not None and later["o"]["seq"] == 1_800_000_000_000


def test_a_rotated_op_cert_signs_again_with_a_higher_seq(tmp_path, keys):
    first, _ = _prep(tmp_path / "hub", keys)
    keys.rotate_op()
    keys = HubKeys.open(tmp_path / "hub")
    after, _ = _prep(tmp_path / "hub", keys)
    assert first is not None and after is not None and after["o"]["seq"] > first["o"]["seq"]
    assert _check(keys, after)["share"] == SHARE


def test_a_damaged_state_file_is_signed_again(tmp_path, keys):
    first, _ = _prep(tmp_path / "hub", keys)
    p = tmp_path / "hub" / PUBLISH_WHERE_FILE
    p.write_text("{nope")
    p.chmod(0o600)
    again, _ = _prep(tmp_path / "hub", keys)
    assert first is not None and again is not None and _check(keys, again)


@pytest.mark.parametrize(
    ("kw", "dev", "word"),
    [
        ({"share": "http://127.0.0.1:8104"}, False, "publish-share-url"),  # loopback, not a dev hub
        ({"share": "https://10.0.0.5"}, False, "publish-share-url"),  # private
        ({"share": "https://10.0.0.5"}, True, "publish-share-url"),  # dev adds loopback only
        ({"share": "https://hub.example.com/x"}, False, "publish-share-url"),
        ({"share": "https://share.example.com", "private": "https://SHARE.example.com:443"}, False, "private"),
        ({"share": PRIVATE}, True, "private"),  # equal to the private --publish-url
        ({"share": "https://share.example.com", "api_host": "127.0.0.1:8103"}, False, "api-host"),
        ({"api_host": None}, True, "needs --api-host"),
    ],
)
def test_refused_origins_serve_nothing_and_say_one_line(tmp_path, keys, kw, dev, word):
    got, lines = _prep(tmp_path / "hub", keys, dev=dev, **{"api_host": HOST if dev else "pub.example.com", **kw})
    assert got is None and len(lines) == 1 and word in lines[0]
    assert not (tmp_path / "hub" / PUBLISH_WHERE_FILE).exists()


@pytest.mark.parametrize("api_host", [None, HOST])
def test_nothing_asked_nothing_said(tmp_path, keys, api_host):
    got, lines = _prep(tmp_path / "hub", keys, api_host=api_host, share=None)
    assert got is None and lines == []


def test_a_real_hub_signs_https_names(tmp_path):
    k = HubKeys.init(tmp_path / "real", real=True)
    got, lines = _prep(
        tmp_path / "real",
        k,
        api_host="Pub.Example.COM",
        share="https://pub.example.com:443",
        private="http://127.0.0.1:1",
        dev=False,
    )
    assert lines == [] and got is not None
    o = _check(k, got, dev=False)
    assert o["server"] == o["share"] == "https://pub.example.com" and o["dev"] is False


# --- over the real link -------------------------------------------------------------------------------------------


def _start(state, up, *extra, invite=None):
    args = [sys.executable, "-m", "orch_hub.cli", "serve", "--state", str(state), "--listen", "127.0.0.1:0"]
    args += ["--publish-url", up.url, "--api-host", HOST, *extra]
    if invite:
        args += ["--invite", invite]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p.stdout
    said: list[str] = []  # the lines before the ready line (a WARN comes first)
    while True:
        line = p.stdout.readline()
        m = READY.fullmatch(line)
        if m:
            break
        assert line, "".join(said) + (p.stderr.read() if p.poll() is not None and p.stderr else "")
        said.append(line)
    p.said = said  # type: ignore[attr-defined]
    return p, int(m[1]), m[2], m[3]


def _stop(p):
    p.send_signal(signal.SIGTERM)  # our own child
    assert p.wait(10) == 0


def _link(port, pin, hub_id, hub_pub, first):
    return LinkClient.open(
        "127.0.0.1",
        port,
        tls_spki_sha256=pin,
        hub_id=hub_id,
        workspace_id=G.WS_A,
        wsk=KS.WskSigner(G.K.priv["wsk_a"]),
        hk_op_pub=hub_pub,
        send_wsk_pub=first,
        timeout=5,
    )


def test_join_and_hub_where_carry_publish_where_and_a_restart_keeps_the_bytes(tmp_path):
    state = tmp_path / "hub"
    keys = HubKeys.init(state)
    SETT.save(state, SETTINGS)
    up = FakeUpstream()
    try:
        p, port, pin, hub_id = _start(state, up, "--publish-share-url", SHARE, invite="inv1")
        try:
            keys = HubKeys.open(state)
            c = _link(port, pin, hub_id, keys.op_signer.public, True)
            body = {
                "invite": "inv1",
                "card": G.v2_card(),
                "pk_pub": b64u(G.K.pub["pk_alice"]),
                "binding": binding(G.K.priv["wsk_a"], keys, G.WS_A),
            }
            joined = c.request("join", body)
            assert set(joined) == {"publish_where"}
            where = c.request("hub_where", {})
            c.close()
        finally:
            _stop(p)
        assert "publish" not in where and where["publish_where"] == joined["publish_where"]
        o = _check(keys, where["publish_where"])
        assert o["server"] == f"http://{HOST}" and o["share"] == SHARE

        p, port, pin, hub_id = _start(state, up, "--publish-share-url", SHARE)
        try:
            c = _link(port, pin, hub_id, keys.op_signer.public, False)
            again = c.request("hub_where", {})
            c.close()
        finally:
            _stop(p)
        assert cj(again["publish_where"]) == cj(where["publish_where"])
    finally:
        up.close()


def test_a_refused_share_url_serves_no_publish_where_and_prints_a_line(tmp_path):
    state = tmp_path / "hub"
    keys = HubKeys.init(state)
    SETT.save(state, SETTINGS)
    up = FakeUpstream()
    try:
        # the share URL is the private --publish-url itself
        p, port, pin, hub_id = _start(state, up, "--publish-share-url", up.url, invite="inv1")
        try:
            assert len(p.said) == 1 and "publish_where not served" in p.said[0]  # type: ignore[attr-defined]
            keys = HubKeys.open(state)
            c = _link(port, pin, hub_id, keys.op_signer.public, True)
            body = {
                "invite": "inv1",
                "card": G.v2_card(),
                "pk_pub": b64u(G.K.pub["pk_alice"]),
                "binding": binding(G.K.priv["wsk_a"], keys, G.WS_A),
            }
            assert c.request("join", body) == {}
            where = c.request("hub_where", {})
            c.close()
        finally:
            _stop(p)
        assert "publish_where" not in where and "publish" not in where
        assert not (state / PUBLISH_WHERE_FILE).exists()
    finally:
        up.close()
