"""T2 for B1 against the REAL orch-relay of ``SOURCE`` (loopback only): a served workspace links through the host hook
(``host.relay_link`` + the genesis pin signed on the person path), the worker reaches ``linked``, the owner client
pairs the reference phone with ``--primary external`` (R6b publishes the membership), the phone joins, does a column-R
``whoami`` and reads, and answers a question through ``relay.submit``; the answer is committed on the host. The R6 gate
is opened by the test only (``R6_READY`` stays False in the code)."""

from __future__ import annotations

import errno
import os
import threading
import time

import pytest

from orch import crypto, devsandbox
from orch.host import paircli
from orch.identity import recovery as rec
from orch.relay import worker as relay_worker
from tests.host.dispatch_helpers import SESSION, Served, ok
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import Lab
from tests.host.test_b1_genesis_pin import Backend
from tests.identity.helpers import Person
from tests.relay.owner import SandboxOwner
from tests.relay.phone import DevPhone, HttpNet
from tests.store import helpers as store_helpers

from .harness import RelayProcess

pytestmark = pytest.mark.relay_t2

FAST = 2**10
MIB4 = 4 << 20


def wait_for(fn, what: str, timeout: float = 90.0, every: float = 0.25):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}: last {last!r}")


class KeyedPerson(Person):
    """A test person whose person key is the sandbox's recovery-code key (so the relay account's PK is the owner's)."""

    def __init__(self, pk) -> None:
        super().__init__()
        self.pk, self.pk_pub = pk, crypto.public_bytes(pk)


class LiveServed(Served):
    """``Served`` on the wall clock (the relay checks times)."""

    def __init__(self, lab: Lab) -> None:
        self.lab = lab
        self.h = lab.host(clock=lambda: int(time.time()))
        self.h.__enter__()
        self.control = self.h.client()
        self.wid = ok(lab.register(self.control))["data"]["workspace_id"]
        self.service = self.h.host.services[self.wid]
        self.clients = []


def test_a_served_workspace_links_pairs_a_phone_and_takes_its_answer(relay: RelayProcess, tmp_path, monkeypatch):
    monkeypatch.setattr(relay_worker, "R6_READY", True)
    # the sandbox person: its recovery code is the workspace owner's person key
    root = tmp_path / "sbx"
    os.mkdir(root, 0o700)
    code = rec.generate_recovery_code()
    pk = rec._key_at(code, FAST)
    devsandbox.write_manifest(root, "cd" * 8, person_id="p_" + crypto.person_id(crypto.public_bytes(pk)).hex())
    os.mkdir(root / "secrets", 0o700)
    fd = os.open(root / "secrets" / "recovery-code", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, code.encode())
    os.close(fd)
    first = []
    monkeypatch.setattr(
        store_helpers, "Person", lambda: KeyedPerson(pk) if not first and not first.append(1) else Person()
    )
    import tests.ops.helpers as ops_helpers

    real_ws = ops_helpers.Ws
    monkeypatch.setattr(ops_helpers, "Ws", lambda p: real_ws(p, live=True))
    import tests.host.lifecycle_helpers as lh

    monkeypatch.setattr(lh, "Ws", ops_helpers.Ws)
    try:
        lab = Lab(tmp_path)
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    owner = SandboxOwner(root, relay.origin, kdf_n=FAST)
    served = None
    try:
        served = LiveServed(lab)
        wid, state = served.wid, lab.state
        call = lambda op, args: _data(served.control.call(op, args))  # noqa: E731
        cert = owner.create_account(relay.admin("invite", "create", "--ttl", "1h").strip())
        # -- link: reserve, genesis pin on the person path, start; the worker reaches `linked`
        call("host.relay_link", {"workspace_id": wid, "origin": relay.origin, "max_publication_bytes": MIB4})
        paircli.sign_genesis_pin(call, wid, backend=Backend(lab.ws.owner), key_id="dk", name="t2",
                                 confirm=lambda text, word: True)  # fmt: skip
        call("host.relay_start", {"workspace_id": wid})
        st = wait_for(
            lambda: (lambda r: r if r.get("link") == "linked" or r.get("worker") == "failed" else None)(
                call("host.relay_status", {"workspace_id": wid})["relay"]
            ),
            "linked",
        )
        assert st["link"] == "linked", st
        call("host.relay_primary_set", {"workspace_id": wid, "cert": __import__("json").loads(cert.read_bytes()),
                                        "label": "t2 primary"})  # fmt: skip
        # -- pair with --primary external: the owner client works the primary's queue
        offer = call("host.relay_pair_offer", {"workspace_id": wid, "scopes": ["look", "decide"]})
        phone = DevPhone(offer["link"]["https"], label="t2 phone")
        net = HttpNet(relay.origin, wid)
        codes: dict[str, str] = {}
        outcome: dict = {}

        def primary() -> None:
            c = served.h.client()  # one control connection per thread
            call = lambda op, args: _data(c.call(op, args))  # noqa: E731
            try:
                req = wait_for(
                    lambda: next((o["outstanding_request"] for o in call("host.relay_pair_list", {"workspace_id": wid})[
                        "offers"] if o["offer_id"] == offer["offer_id"] and o.get("outstanding_request")), None),
                    "the cert request", 120)  # fmt: skip
                got = owner.serve_request(req, wid, state, lambda label, scopes: wait_for(
                    lambda: codes.get("cert_code"), "cert_code", 60), wait_s=120)  # fmt: skip
                outcome["primary"] = got
                if got.cert is not None:
                    call("host.relay_pair_cert", {"workspace_id": wid, "offer_id": offer["offer_id"], "cert": got.cert})
            except BaseException as exc:  # noqa: BLE001
                outcome["primary"] = exc

        def host_side() -> None:
            c = served.h.client()
            call = lambda op, args: _data(c.call(op, args))  # noqa: E731
            try:
                sas = wait_for(lambda: codes.get("sas"), "sas", 60)
                end = time.monotonic() + 120
                while time.monotonic() < end:
                    shown = call("host.relay_pair_show", {"workspace_id": wid, "offer_id": offer["offer_id"]})["offer"]
                    if shown["state"] == "pending":
                        call("host.relay_pair_answer", {"workspace_id": wid, "offer_id": offer["offer_id"], "code": sas,
                                                        "approve": True})  # fmt: skip
                    if (
                        shown["state"] in ("confirmed", "cert_pending")
                        and shown.get("has_cert")
                        and not outcome.get("signed")
                    ):
                        outcome["signed"] = True
                        human = Human(served)
                        prepared = human.prepare("relay.pair_device", {"offer_id": offer["offer_id"]})
                        signed = human.sign(prepared)
                        ok(human.submit_res(prepared["request"]["id"], signed.events))
                    if shown["state"] in ("approved", "rejected", "expired", "cancelled"):
                        outcome["final"] = shown["state"]
                        return
                    time.sleep(0.3)
            except BaseException as exc:  # noqa: BLE001
                outcome["host"] = exc

        threads = [threading.Thread(target=f, daemon=True) for f in (primary, host_side)]
        for t in threads:
            t.start()
        try:
            approved = phone.pair(net, codes=lambda o, ph, c: codes.__setitem__(ph, c), deadline_s=60, poll_s=0.5)
        except BaseException as exc:  # noqa: BLE001 - the state at the stall is the diagnosis
            shown = call("host.relay_pair_show", {"workspace_id": wid, "offer_id": offer["offer_id"]})["offer"]
            raise AssertionError(
                f"pairing stalled: {exc!r}; phone {phone.a.state} codes {sorted(codes)} host offer {shown} "
                f"outcome {outcome} status {call('host.relay_status', {'workspace_id': wid})['relay']}"
            ) from exc
        for t in threads:
            t.join(30)
        assert not isinstance(outcome.get("primary"), BaseException), outcome
        assert "host" not in outcome, outcome
        assert outcome.get("final") == "approved" and approved.scopes == ("look", "decide")
        # -- the phone joins (R6b published the membership), reads, and answers
        wks = wait_for(lambda: _join(phone, net), "the phone's join", 120)
        assert phone.scopes == ("look", "decide") and wks
        meta, data = phone.whoami(net)
        assert meta == {"status": "ok"}, (meta, data)
        relay_caps = _find(data, "relay")
        assert relay_caps["r6_ready"] is True and relay_caps["ops"], data
        uid, key = served.ticket("T2 ticket")
        cli = served.client()
        res = ok(cli.req("ask", {"ref": key, "text": "Ship it?", "options": ["yes", "no"], "rec": "yes"},
                         grant=served.grant, session=SESSION, idem=True))  # fmt: skip
        assert _find(res, "question") == "Q1", res
        meta, tickets = phone.call(net, {"op": "ticket.get", "args": {"ref": key}})
        assert meta["status"] == "ok", (meta, tickets)
        view = _find(tickets, "questions", container=True)
        qhash, based_on = view["questions"][0]["hash"], view["doc"]["head"]["head"]
        meta, out = phone.answer(net, uid, "Q1", qhash, "yes", based_on=based_on)
        assert meta.get("status") == "ok", (meta, out)
        events = wait_for(lambda: [e for e in lab.ws.read_events(uid) if e["type"] == "question.answered"],
                          "the answer on the host", 60)  # fmt: skip
        assert events[0]["actor"]["device"] == "d_" + phone.device_id
    finally:
        owner.close()
        if served is not None:
            served.close()
        lab.close()


def _data(res: dict) -> dict:
    assert res["ok"] is True, res
    return res["result"]["data"]


def _join(phone: DevPhone, net: HttpNet):
    from tests.relay.phone import PhoneError

    try:
        return phone.join(net)
    except PhoneError as exc:
        if exc.code in ("not_a_member", "below_anchor", "no_grant"):
            return None
        raise


def _find(obj, key, *, container=False):
    """The dict that holds ``key`` in a nested answer (the bridge wraps the op's data), or its value."""
    if isinstance(obj, dict):
        if key in obj:
            return obj if container else obj[key]
        for v in obj.values():
            got = _find(v, key, container=container)
            if got is not None:
                return got
    return None
