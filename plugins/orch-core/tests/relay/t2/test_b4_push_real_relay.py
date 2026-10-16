"""T2 for B4 against the REAL orch-relay (loopback): a served workspace linked, a phone paired and joined, the phone
registers a (fake) APNs token; a question on the host becomes a sealed push, the host POSTs it with ``Orch-Holder``,
the relay delivers it to an in-test fake APNs gateway (the exact body and headers), and the outer opens with the
phone's ``WK`` (``orch.crypto.push.open_push``): kind ``question`` with the question's text as label; answering on the
host sends ``question.closed``. A relay without push answers 404 and the host reports ``push.state = off_at_relay``
without an alarm.

The relay has no gateway setting (production must not be redirectable); the test starts it through
``orch-relay-t2-apns.py``, which patches the transport seam the relay's own tests patch (plain HTTP/1.1 to the fake).
Run against an unmerged relay branch with ``ORCH_RELAY_T2_ALLOW_DESCENDANT=1`` (local only)."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orch import canon
from orch.crypto import push as cpush
from orch.relay import push as host_push
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import Human
from tests.relay.phone import PhoneError

from . import harness
from .harness import RelayProcess, relay_dir, start_relay
from .test_r5_pairing_real_relay import _data, _find, paired_world, wait_for

pytestmark = pytest.mark.relay_t2

ENTRY = Path(__file__).resolve().parent / "orch-relay-t2-apns.py"
TOPIC = "io.severin.orch"


class FakeApns:
    """A loopback gateway that records every ``POST /3/device/<token>`` and answers 200."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, dict[str, str], bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.hits.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _secret_file(path: Path, data: bytes) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, data)
    os.close(fd)
    return str(path)


@pytest.fixture
def push_relay(tmp_path_factory: pytest.TempPathFactory):
    """The relay with push ENABLED (sandbox key, token secret, fake gateway), all key files 0600 in a temp directory."""
    d = relay_dir()
    data = tmp_path_factory.mktemp("relay-data")
    keys = tmp_path_factory.mktemp("push-keys")
    os.chmod(keys, 0o700)
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fake = FakeApns()
    extra = {
        "ORCH_RELAY_APNS_TEAM_ID": "T2TEAMID01",
        "ORCH_RELAY_APNS_TOPIC": TOPIC,
        "ORCH_RELAY_PUSH_TOKEN_KEY_FILE": _secret_file(keys / "push-token.key", secrets.token_bytes(32)),
        "ORCH_RELAY_APNS_SANDBOX_KEY_ID": "T2KEYID001",
        "ORCH_RELAY_APNS_SANDBOX_KEY_FILE": _secret_file(keys / "apns-sandbox.p8", pem),
        "ORCH_T2_FAKE_APNS": fake.url,
    }
    rp = start_relay(d, data, harness.INTERNAL_CAP, extra_env=extra, entry=ENTRY)
    rp.fake = fake  # type: ignore[attr-defined]
    try:
        yield rp
    finally:
        fake.close()
        leaked = rp.finish()
    assert not leaked, f"relay teardown leaked (reaped): {leaked}"


@pytest.fixture
def plain_relay(tmp_path_factory: pytest.TempPathFactory):
    """The relay with NO push configuration at all."""
    rp = start_relay(relay_dir(), tmp_path_factory.mktemp("relay-data"), harness.INTERNAL_CAP)
    try:
        yield rp
    finally:
        leaked = rp.finish()
    assert not leaked, f"relay teardown leaked (reaped): {leaked}"


def _ask(w, text: str) -> tuple[str, str]:
    uid, key = w.served.ticket("T2 push ticket")
    res = ok(w.served.client().req("ask", {"ref": key, "text": text, "options": ["yes", "no"], "rec": "yes"},
                                   grant=w.served.grant, session=SESSION, idem=True))  # fmt: skip
    assert str(_find(res, "question")).startswith("Q"), res
    return uid, key


def _opened(w, fake: FakeApns, n: int, token: bytes, what: str) -> list[dict]:
    """Wait for ``n`` deliveries, check each one's path, headers and body, and open its outer with the phone's keys."""
    wait_for(lambda: len(fake.hits) >= n, what, 60, every=0.2)
    out = []
    for path, headers, body in fake.hits[:n]:
        assert path == "/3/device/" + token.hex()
        assert headers["apns-topic"] == TOPIC and headers["apns-push-type"] == "alert"
        assert headers["authorization"].startswith("bearer ") and "apns-collapse-id" not in headers
        doc = json.loads(body)
        assert set(doc) == {"aps", "o"} and doc["aps"] == {
            "alert": {"title": "orch", "body": "New activity in orch"},
            "mutable-content": 1,
        }
        raw = doc["o"].encode()
        outer = canon.loads_strict(raw)
        assert set(outer) == {"v", "ws", "epoch", "c"} and outer["ws"] == w.wid
        keys = {w.wid: cpush.PushKeys(w.phone.a.wsk_pub, w.phone.wk)}
        out.append(cpush.open_push(raw, keys, int(time.time() * 1000)))
    return out


def test_a_question_and_its_answer_reach_the_fake_apns(push_relay: RelayProcess, tmp_path, monkeypatch):
    fake: FakeApns = push_relay.fake  # type: ignore[attr-defined]
    # the host sends one push per question id per minute (ID_INTERVAL_MS); the closed push would wait out the minute
    monkeypatch.setattr(host_push, "ID_INTERVAL_MS", 1500)
    with paired_world(push_relay, tmp_path, monkeypatch) as w:
        token = secrets.token_bytes(32)
        got = w.net.push_register(secrets.token_hex(16), token)
        assert got["registered"] is True
        uid, key = _ask(w, "Ship it today?")
        (p,) = _opened(w, fake, 1, token, "the question push")
        assert p["kind"] == "question" and p["label"].rstrip() == "Ship it today?" and p["id"]
        qid = p["id"]
        # the answer on the HOST (the phone would be excluded as the answering device): a question.closed push
        human = Human(w.served)
        prepared = human.prepare("answer", {"ref": key, "question": "Q1", "option": "yes"})
        signed = human.sign(prepared)
        ok(human.submit_res(prepared["request"]["id"], signed.events))
        closed = _opened(w, fake, 2, token, "the question.closed push")[1]
        assert closed["kind"] == "question.closed" and closed["id"] == qid
        st = _data(w.served.control.call("host.relay_status", {"workspace_id": w.wid}))["relay"]
        assert st["push"]["state"] == "on" and not [a for a in st["alarms"] if "push" in json.dumps(a)], st


def test_a_relay_without_push_is_off_at_relay_and_raises_no_alarm(plain_relay: RelayProcess, tmp_path, monkeypatch):
    with paired_world(plain_relay, tmp_path, monkeypatch) as w:
        with pytest.raises(PhoneError, match="404 not_found"):
            w.net.push_register(secrets.token_hex(16), secrets.token_bytes(32))
        for i in range(3):  # a storm of events must not become a storm of alarms
            _ask(w, f"Question number {i}?")

        def off() -> dict | None:
            st = _data(w.served.control.call("host.relay_status", {"workspace_id": w.wid}))["relay"]
            return st if st.get("push", {}).get("state") == "off_at_relay" else None

        st = wait_for(off, "push off_at_relay", 60, every=0.3)
        time.sleep(1.0)
        st = _data(w.served.control.call("host.relay_status", {"workspace_id": w.wid}))["relay"]
        assert st["push"]["state"] == "off_at_relay"
        assert not [a for a in st["alarms"] if "push" in json.dumps(a)], st["alarms"]
