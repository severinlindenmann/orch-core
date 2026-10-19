"""R6c-2: ``orch host relay pair ... --primary external`` and ``orch host relay primary set`` (11A.15.9): the attached
loop (typed SAS with 3 attempts, the external primary's wait, step 9 once the certificate is in hand, progress), the
link rules (terminal or ``--json-fd`` only; never on resume), the refusals (``ORCH_GRANT``, the unbuilt inline
primary), and the subcommands over a real host's control socket. Fakes for the relay and step 9's signature; never a
real relay."""

from __future__ import annotations

import argparse
import errno
import fcntl
import io
import json
import os

import pytest

from orch import crypto
from orch.host import paircli, relaypair, relayr6
from orch.host.custody import write_private
from orch.host.relaylink import link_path
from orch.identity import pairing
from tests.host.lifecycle_helpers import Lab
from tests.identity.helpers import Person
from tests.relay.pairkit import FakeMembership, FakeRelay, Phone, Runner, make_cert, make_challenge

ORIGIN = "https://relay.example"


def ns(**kw):
    base = dict(action="pair", sub=None, workspace=None, scopes=None, ttl=None, json=False, json_fd=None, no_qr=True,
                resume=False, offer=None, primary="external", cert=None, label=None)  # fmt: skip
    base.update(kw)
    return argparse.Namespace(**base)


# -- the attached loop, unit ---------------------------------------------------------------------------------------


class Script:
    """A fake control socket whose ``show`` answers follow a script; ``answer`` records the typed codes."""

    def __init__(self, states):
        self.states = list(states)
        self.answers = []

    def __call__(self, op, args):
        if op == "host.relay_pair_show":
            st = self.states[0] if len(self.states) == 1 else self.states.pop(0)
            return {"offer": {"state": st[0], "has_cert": st[1], "label": "Sev's phone", "scopes": ["look", "decide"],
                              "second_key": st[2] if len(st) > 2 else False, "expires_ms": 1}}  # fmt: skip
        if op == "host.relay_pair_answer":
            self.answers.append((args["code"], args["approve"]))
            left = 3 - len(self.answers)
            return {"state": "pending" if args["code"] != "RIGHTA" else "cert_pending", "attempts_left": left}
        raise AssertionError(op)


def test_attach_types_the_sas_waits_for_the_primary_signs_step_9_and_ends_approved():
    s = Script([("open", False), ("pending", False, True), ("pending", False), ("cert_pending", False),
                ("cert_pending", True), ("allocated", True), ("approved", True)])  # fmt: skip
    codes, signed, out = iter(["WRONGA", "RIGHTA"]), [], io.StringIO()
    rc = paircli.attach(
        s, "w" * 32, "o" * 32, out, ask=lambda p: next(codes), step9=signed.append, sleep=lambda t: None
    )
    text = out.getvalue()
    assert rc == 0 and s.answers == [("WRONGA", True), ("RIGHTA", True)] and signed == ["o" * 32]
    assert "someone else has this link" in text and "named by the device" in text
    assert "that is not the code: 2 tries left" in text and "waiting for the primary" in text
    assert "approved:" in text


class InlineScript(Script):
    """``Script`` plus the cert hand-over: ``relay_pair_cert`` flips ``has_cert``; ``cancel`` ends the offer."""

    def __init__(self, states):
        super().__init__(states)
        self.certs, self.cancelled = [], []

    def __call__(self, op, args):
        if op == "host.relay_pair_cert":
            self.certs.append(args)
            return {"state": "cert_pending"}
        if op == "host.relay_pair_cancel":
            self.cancelled.append(args["offer_id"])
            self.states = [("cancelled", False)]
            return {"state": "cancelled"}
        got = super().__call__(op, args)
        if op == "host.relay_pair_show":
            got["offer"]["outstanding_request"] = "r" * 32 if got["offer"]["state"] == "cert_pending" else None
            got["offer"]["device_id"] = "d" * 32
        return got


class Outcome:
    def __init__(self, cert=None, rejection=None, device_id=None):
        self.cert, self.rejection, self.device_id = cert, rejection, device_id


def test_attach_inline_primary_runs_once_and_hands_the_certificate_to_r6():
    s = InlineScript([("pending", False), ("cert_pending", False), ("cert_pending", False),
                      ("cert_pending", True), ("approved", True)])  # fmt: skip
    seen, signed, out = [], [], io.StringIO()

    def primary(offer_id, rid, offer):
        seen.append((offer_id, rid, offer["device_id"]))
        return Outcome(cert={"o": {}, "sig": "x"})

    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=signed.append,
                        sleep=lambda t: None, primary=primary)  # fmt: skip
    assert rc == 0 and seen == [("o" * 32, "r" * 32, "d" * 32)] and signed == ["o" * 32]
    assert s.certs == [{"workspace_id": "w" * 32, "offer_id": "o" * 32, "cert": {"o": {}, "sig": "x"}}]
    assert "waiting for the primary" not in out.getvalue()


def test_attach_inline_rejection_goes_to_r6_as_a_rejection():
    s = InlineScript([("pending", False), ("cert_pending", False), ("rejected", False)])
    out = io.StringIO()
    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=lambda *a: Outcome(rejection={"o": {}, "sig": "r"}))  # fmt: skip
    assert rc == 3 and s.certs[0]["rejection"] == {"o": {}, "sig": "r"} and "cert" not in s.certs[0]


def test_attach_inline_failure_cancels_the_offer_and_never_hangs():
    s = InlineScript([("pending", False), ("cert_pending", False)])
    out = io.StringIO()

    def primary(*a):
        raise RuntimeError("secret-looking text from somewhere")

    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=primary, max_polls=50)  # fmt: skip
    text = out.getvalue()
    assert rc == 3 and s.cancelled == ["o" * 32] and s.certs == []
    assert "RuntimeError" in text and "secret-looking" not in text and "start a new one" in text


def test_attach_inline_interrupt_leaves_the_offer_for_resume():
    s = InlineScript([("pending", False), ("cert_pending", False)])

    def primary(*a):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        paircli.attach(s, "w" * 32, "o" * 32, io.StringIO(), ask=lambda p: "ABCDEF", step9=lambda o: None,
                       sleep=lambda t: None, primary=primary)  # fmt: skip
    assert s.cancelled == []


def test_attach_waits_for_the_request_id_before_the_inline_step():
    s = InlineScript([("pending", False), ("cert_pending", False)])
    calls, shown = [], []
    real = s.__call__

    def hidden(op, args):  # the host reports no outstanding request yet on the first cert_pending poll
        got = real(op, args)
        if op == "host.relay_pair_show" and got["offer"]["state"] == "cert_pending":
            shown.append(1)
            if len(shown) == 1:
                got["offer"]["outstanding_request"] = None
        return got

    def primary(*a):
        calls.append(a)
        return Outcome(rejection={"o": {}, "sig": "r"})

    paircli.attach(hidden, "w" * 32, "o" * 32, io.StringIO(), ask=lambda p: "ABCDEF", step9=lambda o: None,
                   sleep=lambda t: None, primary=primary, max_polls=3)  # fmt: skip
    assert len(calls) == 1 and len(shown) >= 2


def test_sas_input_that_is_not_a_code_is_never_sent():
    s = Script([("pending", False), ("pending", False), ("pending", False), ("rejected", False)])
    typed = iter(["one two three", "abc", "not a code"])
    out = io.StringIO()
    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: next(typed), step9=lambda o: None,
                        sleep=lambda t: None)  # fmt: skip
    text = out.getvalue()
    assert rc == 3 and s.answers == [("", False)]  # after 3 unusable entries: a reject, never the text
    assert text.count("that did not look like the phone's code; nothing was sent") == 3
    assert "never type your recovery code here" in text


def test_sas_is_normalised_before_it_is_sent():
    s = Script([("pending", False), ("cert_pending", False)])
    paircli.attach(s, "w" * 32, "o" * 32, io.StringIO(), ask=lambda p: "  abcd23 ", step9=lambda o: None,
                   sleep=lambda t: None, max_polls=1)  # fmt: skip
    assert s.answers == [("ABCD23", True)]


def test_the_two_prompts_are_worded_differently():
    from orch.relay_owner.inline import CERT_CODE_PROMPT

    assert paircli.SAS_PROMPT == "pairing code: type the 6 characters the phone shows (empty: reject): "
    assert CERT_CODE_PROMPT == "compare code: type the 6 characters the phone shows now (empty: reject): "
    assert "step" not in paircli.SAS_PROMPT + CERT_CODE_PROMPT


def test_attach_empty_code_rejects_and_a_terminal_state_ends_with_3():
    s = Script([("pending", False), ("rejected", False)])
    out = io.StringIO()
    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "", step9=lambda o: None, sleep=lambda t: None)
    assert rc == 3 and s.answers == [("", False)] and "the pairing ended: rejected" in out.getvalue()


def test_ttl_parsing():
    assert paircli.parse_ttl("10m") == 600_000 and paircli.parse_ttl("90s") == 90_000
    for bad in ("11m", "30s", "x"):
        with pytest.raises(Exception):  # noqa: B017 - HostError invalid.input
            paircli.parse_ttl(bad)


# -- over a real host ------------------------------------------------------------------------------------------------


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


@pytest.fixture
def host(lab, monkeypatch):
    with lab.host() as h, h.client() as c:
        from orch.host.control_client import new_idem

        res = c.call("host.register", {"root": lab.root, "peers": lab.peers()}, idem=new_idem())
        wid = res["result"]["data"]["workspace_id"]
        service = h.host.services[wid]
        lp = link_path(lab.state, wid)
        lp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(lp, {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": "linked"})
        relayr6.keys_for(service)
        monkeypatch.setattr(relaypair.HostPairing, "offer_blocked", lambda self: None)
        engine = relaypair.engine_for(service)
        engine.routes, engine.membership, engine.post = FakeRelay(), FakeMembership(), Runner()
        owner = lab.ws.owner
        primary = Person()
        primary.pk, primary.pk_pub = owner.pk, owner.pk_pub
        try:
            yield lab, h, wid, service, engine, owner, primary
        finally:
            relaypair.close(service)
            relayr6.close(service)


def run(lab, h, args, env=None, **kw):
    out = io.StringIO()
    rc = paircli.main(args, env or {}, out, state=lab.state, control=h.host.control_path, **kw)
    return rc, out.getvalue()


def _registered_primary(lab, owner, tmp_path, monkeypatch, *, origin=ORIGIN, device_of=None):
    """The install's primary as ``orch keys primary init`` leaves it: keys, a certificate for the on-disk primary
    device signed by the workspace owner's person key, ``cert.json`` and ``relay.json``."""
    from orch.custody import KdfParams, PassphraseBackend, primary_register
    from orch.custody import primary as install_primary
    from orch.identity import certs

    fast = KdfParams(n=2**10)
    monkeypatch.setattr(
        install_primary, "open_backend",
        lambda d: PassphraseBackend(d, _passphrase_provider=lambda r: "Zq7!mPx2-vL9#rTb4w", _kdf=fast, _min_n=2**10),
    )  # fmt: skip
    if not install_primary.keys_dir(lab.state).is_dir():
        install_primary.init(lab.state)
    info = install_primary.load(lab.state)
    cert = certs.make_device_cert(
        owner.pk_pub, owner.pk_sign, dk_sig_pub=device_of or info.dk_sig_pub, dk_kx_pub=info.dk_kx_pub,
        label_sealed=b"x" * 40, created_ms=1_000, expires_ms=None, scopes_max=["look"],
    )  # fmt: skip
    pid = crypto.person_id(owner.pk_pub).hex()
    primary_register._write_private(
        primary_register.cert_path(lab.state),
        {"v": 1, "registered": True, "origin": origin, "label": "Mac (primary)", "cert": cert},
    )
    primary_register._write_private(
        primary_register.relay_path(lab.state),
        {"v": 1, "origin": origin, "person_id": pid, "device_id": cert["o"]["device_id"], "label": "Mac (primary)"},
    )
    return cert


def test_primary_set_defaults_to_the_registered_primary(host, tmp_path, monkeypatch):
    lab, h, wid, service, engine, owner, primary = host
    cert = _registered_primary(lab, lab.ws.owner, tmp_path, monkeypatch)
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid))
    assert rc == 0 and json.loads(text)["data"]["device_id"] == cert["o"]["device_id"], text


def test_primary_set_default_refusals(host, tmp_path, monkeypatch):
    lab, h, wid, service, engine, owner, primary = host
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid))
    assert rc != 0 and "primary.not_registered" in text  # nothing registered yet
    _registered_primary(lab, lab.ws.owner, tmp_path, monkeypatch, origin="https://other.example")
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid))
    assert rc != 0 and "primary.relay_mismatch" in text  # the workspace is linked to ORIGIN
    stranger = Person()  # a certificate of another person under the same on-disk device
    _registered_primary(lab, stranger, tmp_path, monkeypatch)
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid))
    assert rc != 0 and "owner's person" in text
    _registered_primary(lab, lab.ws.owner, tmp_path, monkeypatch, device_of=Person().sig_pub)
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid))
    assert rc != 0 and "not for this machine's primary" in text


def test_link_defaults_to_the_stored_relay_and_refuses_another(tmp_path, monkeypatch):
    from orch.custody import primary_register

    st = tmp_path / "state"
    (st / "person" / "primary").mkdir(parents=True, mode=0o700)
    seen = []

    def call(op, args):
        seen.append(args["origin"])
        raise paircli._Refused({"code": "stop"}, 1)

    entry = {"workspace_id": "w" * 32}
    a = lambda **kw: argparse.Namespace(**{"relay": None, "max_publication_bytes": 1 << 20, "ca_file": None,  # noqa: E731
                                           "proxy": None, **kw})  # fmt: skip
    with pytest.raises(Exception, match="--relay"):  # nothing stored and nothing given
        paircli._link_run(call, entry, st, a(), io.StringIO(), lambda t: None)
    primary_register._write_private(
        primary_register.relay_path(st), {"v": 1, "origin": ORIGIN, "person_id": "0" * 32, "device_id": "1" * 32,
                                          "label": "x"})  # fmt: skip
    with pytest.raises(paircli._Refused):
        paircli._link_run(call, entry, st, a(), io.StringIO(), lambda t: None)
    with pytest.raises(paircli._Refused):
        paircli._link_run(call, entry, st, a(relay=ORIGIN), io.StringIO(), lambda t: None)
    assert seen == [ORIGIN, ORIGIN]
    with pytest.raises(Exception, match="one relay per install"):
        paircli._link_run(call, entry, st, a(relay="https://other.example"), io.StringIO(), lambda t: None)
    assert seen == [ORIGIN, ORIGIN]  # nothing reached the host


def test_primary_set_from_a_file_then_list_shows_no_offer(host, tmp_path):
    lab, h, wid, service, engine, owner, primary = host
    f = tmp_path / "primary-cert.json"
    f.write_text(json.dumps(primary.cert(scopes=("look",))))
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid, cert=str(f), label="Mac"))
    assert rc == 0 and json.loads(text)["data"]["device_id"] == primary.device[2:]
    rc, text = run(lab, h, ns(sub="list", workspace=wid, json=True))
    assert rc == 0 and json.loads(text)["data"]["offers"] == []


@pytest.mark.parametrize(
    "kw, env, code",
    [
        ({}, {"ORCH_GRANT": "g"}, "human_only"),
        ({"primary": "inline"}, {}, "relay.no_primary"),  # inline is built (B1b); the host has no primary set here
        ({"primary": None}, {}, "relay.no_primary"),  # the default is inline
        ({"json": True}, {}, "invalid.input"),  # stdout is no TTY and no --json-fd
        ({"sub": "cancel"}, {}, "invalid.input"),  # needs --offer
        ({"sub": "nope"}, {}, "invalid.input"),
        ({"resume": True}, {}, "invalid.input"),
    ],
)
def test_refusals(host, kw, env, code):
    lab, h, wid, *_ = host
    rc, text = run(lab, h, ns(workspace=wid, **kw), env=env)
    assert rc != 0 and code in text


def test_pair_external_end_to_end_through_the_cli(host):
    """The CLI makes the offer and prints the link; the phone (scripted through the CLI's sleep) uses it; the person
    types the code; the external primary's certificate arrives; step 9 is driven once. Ended by cancel here (step 9's
    real signature is the person path's, covered in test_relay_pair.py)."""
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    out = io.StringIO()
    state = {"phone": None, "step": 0}

    def link_of() -> str:
        line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("link: "))
        return line[len("link: ") :]

    def sleep(_t):
        state["step"] += 1
        if state["phone"] is None:
            phone = Phone(link_of())
            state["phone"] = phone
            engine.receive(*phone.envelope(phone.meta(), ts=engine.now_ms()))
        elif engine.store.get(phone_offer())["state"] == "cert_pending" and not engine.store.get(phone_offer())["cert"]:
            req = engine.routes.requests[-1]
            ch = make_challenge(req["o"], state["phone"], owner.pk)
            cert = make_cert(state["phone"], pk_priv=owner.pk, created_ms=engine.now_ms() - 1000)
            engine.pair_cert(phone_offer(), cert=cert, challenge=ch)

    def phone_offer() -> str:
        return state["phone"].offer

    def ask(prompt):
        rec = engine.store.get(phone_offer())
        p = state["phone"]
        return pairing.sas(wid, p.offer, p.sig_pub, p.kx_pub, engine.ctx.wsk_pub, crypto.unb64u(rec["sas_nonce"]))

    signed = []

    def step9(offer_id):
        signed.append(offer_id)
        engine.cancel(offer_id)  # stand-in: ends the loop (no intent recorded, so cancel is allowed)

    rc = paircli.main(ns(workspace=wid), {}, out, state=lab.state, control=h.host.control_path, ask=ask, step9=step9,
                      sleep=sleep)  # fmt: skip
    text = out.getvalue()
    assert rc == 3 and signed == [phone_offer()]
    assert text.count("orch://pair?relay=") == 1 and "fallback: your relay operator's page can read it" in text
    for needle in (
        "state: open",
        "state: pending",
        "state: cert_pending",
        "waiting for the primary",
        "state: cancelled",
    ):
        assert needle in text, needle
    rec = engine.store.get(phone_offer())
    rest = [ln for ln in text.splitlines() if not ln.startswith(("link: ", "fallback: "))]
    assert rec["secret"] not in "\n".join(rest)  # S only inside the two link lines


def test_json_to_a_private_descriptor_carries_the_link_and_stdout_does_not(host):
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    r, w = os.pipe()
    try:
        out = io.StringIO()
        polls = {"n": 0}

        def sleep(_t):
            polls["n"] += 1
            if polls["n"] == 2:
                oid = engine.list_offers()[0].offer_id
                engine.cancel(oid)

        rc = paircli.main(ns(workspace=wid, json=True, json_fd=w), {}, out, state=lab.state,
                          control=h.host.control_path, sleep=sleep)  # fmt: skip
        os.close(w)
        w = -1
        lines = [json.loads(x) for x in os.read(r, 1 << 16).decode().splitlines()]
    finally:
        os.close(r)
        if w != -1:
            os.close(w)
    assert rc == 3 and lines[0]["native_link"].startswith("orch://") and "link" in lines[0]
    assert [x["state"] for x in lines[1:]] == ["open", "cancelled"]
    assert "orch://" not in out.getvalue() and "#v2." not in out.getvalue()


def test_resume_prints_no_link(host):
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    hd = engine.offer()
    engine.cancel(hd.offer_id)
    rc, text = run(lab, h, ns(workspace=wid, resume=True, offer=hd.offer_id), sleep=lambda t: None)
    assert rc == 3 and "orch://" not in text and "state: cancelled" in text


def test_step_9_runs_the_person_path_with_the_terminal_signer(tmp_path, monkeypatch):
    """The production step-9 driver: ``signer.run`` of ``relay.pair_device {offer_id}`` with the device key's own
    backend (its ``auth``: passphrase, never convenience) on the pinned workspace connection."""
    import orch.hostclient as hc
    from orch.host import signer
    from orch.ops import human

    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / human.KEY_FILE).write_text("{}")
    seen = {}

    class Conn:
        def close(self):
            seen["closed"] = True

    class Backend:
        auth = "passphrase"

        def public_key(self, key_id):
            return Person().sig_pub

    monkeypatch.setattr(hc, "_connect", lambda target: Conn())
    monkeypatch.setattr(human, "keys_dir", lambda state, wid: keys)
    monkeypatch.setattr(human, "open_backend", lambda d: Backend())
    monkeypatch.setattr(
        signer, "run", lambda call, op, args, **kw: seen.update(op=op, args=args, auth=kw["backend"].auth)
    )
    peers = {str(os.getuid()): {"person": "p_" + "b" * 32}}
    entry = {"workspace_id": "a" * 32, "host_uid": os.getuid(), "peers": peers, "root": str(tmp_path), "prefix": "ACME"}
    paircli._step9({}, tmp_path, entry)("c" * 32)
    assert seen == {"op": "relay.pair_device", "args": {"offer_id": "c" * 32}, "auth": "passphrase", "closed": True}


def test_the_host_cli_parses_pair_and_primary_and_routes_them(tmp_path):
    """``orch host relay pair|primary`` through the real argparse front: ORCH_GRANT refused before anything, a list
    with no host is host.unavailable, stop/start take no subcommand."""
    from orch.host.cli import main
    from orch.host.registry import registry_path

    state = tmp_path / "state"
    reg = registry_path(state)
    reg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(reg, {"v": 1, "workspaces": [{"workspace_id": "a" * 32, "prefix": "DEMO", "root": "/x"}]})
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(state), "ORCH_HOST_RUN_DIR": str(tmp_path / "r")}
    out = io.StringIO()
    code = main(["relay", "pair", "--workspace", "DEMO", "--state", str(state), "--primary", "external"],
                env={**env, "ORCH_GRANT": "x"}, out=out)  # fmt: skip
    assert code != 0 and "human_only" in out.getvalue()
    out = io.StringIO()
    code = main(["relay", "pair", "list", "--workspace", "DEMO", "--state", str(state), "--json"], env=env, out=out)
    assert code == 9 and "host.unavailable" in out.getvalue()
    out = io.StringIO()
    code = main(["relay", "stop", "list", "--workspace", "DEMO", "--state", str(state)], env=env, out=out)
    assert code != 0 and "invalid.input" in out.getvalue()


# -- output injection (orch-07 scan): nothing from the relay or a phone reaches the terminal raw -----------------------

EVIL = ["Sev\x1b[2J\x1b[Hphone", "Sev‮enohp", "Sev\nscopes: look, decide, operate", "​Sev\x07\x9b31m",
        "x" * 500]  # fmt: skip
BAD_CHARS = ["\x1b", "‮", "​", "\x07", "\x9b"]


@pytest.mark.parametrize("label", EVIL)
def test_a_device_label_is_terminal_safe_in_the_attached_loop(label):
    class S(Script):
        def __call__(self, op, args):
            if op == "host.relay_pair_show":
                st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                return {"offer": {"state": st, "has_cert": False, "label": label, "scopes": ["look", "decide\x1b[2J"],
                                  "second_key": False, "expires_ms": 1}}  # fmt: skip
            return super().__call__(op, args)

    out = io.StringIO()
    paircli.attach(S(["pending", "approved"]), "w" * 32, "o" * 32, out, ask=lambda p: "", step9=lambda o: None,
                   sleep=lambda t: None)  # fmt: skip
    text = out.getvalue()
    assert not any(c in text for c in BAD_CHARS)
    request_lines = [ln for ln in text.splitlines() if ln.startswith("request from:")]
    assert len(request_lines) == 1 and len(request_lines[0]) < 160  # one line, capped: no injected second line
    assert not any(ln.startswith("scopes: look, decide, operate") for ln in text.splitlines())
    assert "scopes: look, ?" in text  # a scope the host never sends is shown as ?


def test_list_output_and_relay_errors_are_terminal_safe(host):
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    hd = engine.offer()
    phone = Phone(hd.link.native)
    engine.receive(*phone.envelope(phone.meta(label=EVIL[0] + EVIL[1]), ts=engine.now_ms()))
    rc, text = run(lab, h, ns(sub="list", workspace=wid))
    assert rc == 0 and not any(c in text for c in BAD_CHARS)
    rc, text = run(lab, h, ns(sub="list", workspace=wid, json=True))
    assert not any(c in text for c in BAD_CHARS)  # the JSON encoder escapes them


def test_an_error_text_with_control_characters_is_cleaned(monkeypatch, host):
    lab, h, wid, *_ = host

    def boom(state, given):
        raise HostError_("relay.pair_closed", "bad\x1b[2J‮line\nnext", hint="hint\x1b[31m")

    from orch.host.errors import HostError as HostError_

    monkeypatch.setattr(paircli, "_workspace", boom)
    rc, text = run(lab, h, ns(workspace=wid))
    assert rc != 0 and not any(c in text for c in BAD_CHARS) and text.count("\n") == 1


def test_a_refusal_from_the_host_is_json_with_every_control_character_escaped(monkeypatch, host):
    lab, h, wid, *_ = host

    def caller(client):
        def call(op, args):
            raise paircli._Refused({"code": "relay.pair_closed", "message": "x\x1b[2J\u202e\nfake: ok"}, 3)

        return call

    monkeypatch.setattr(paircli, "_caller", caller)
    rc, text = run(lab, h, ns(sub="list", workspace=wid))
    assert rc == 3 and not any(c in text for c in BAD_CHARS) and text.count("\n") == 1
    assert json.loads(text)["error"]["message"].startswith("x")


def test_a_link_that_is_not_printable_ascii_is_never_printed():
    with pytest.raises(Exception):  # noqa: B017 - HostError internal
        paircli._link("orch://pair?relay=x#v2.\x1b[2J")
    assert paircli.safe("a\x1b[2Jb‮c\nd") == "a[2Jbc d"


# -- Opus review of #589: M1 (step 9's errors never escape as tracebacks), L4 (--json-fd) ----------------------------


def _drive_to_step9(host, step9):
    """The CLI from the offer to step 9 (a scripted phone and external primary), with ``step9`` as the driver."""
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    out = io.StringIO()
    state = {"phone": None}

    def sleep(_t):
        if state["phone"] is None:
            line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("link: "))
            state["phone"] = Phone(line[len("link: ") :])
            engine.receive(*state["phone"].envelope(state["phone"].meta(), ts=engine.now_ms()))
            return
        rec = engine.store.get(state["phone"].offer)
        if rec["state"] == "cert_pending" and not rec["cert"]:
            req = engine.routes.requests[-1]
            ch = make_challenge(req["o"], state["phone"], owner.pk)
            cert = make_cert(state["phone"], pk_priv=owner.pk, created_ms=engine.now_ms() - 1000)
            engine.pair_cert(state["phone"].offer, cert=cert, challenge=ch)

    def ask(prompt):
        p = state["phone"]
        rec = engine.store.get(p.offer)
        return pairing.sas(wid, p.offer, p.sig_pub, p.kx_pub, engine.ctx.wsk_pub, crypto.unb64u(rec["sas_nonce"]))

    rc = paircli.main(ns(workspace=wid), {}, out, state=lab.state, control=h.host.control_path, ask=ask, step9=step9,
                      sleep=sleep)  # fmt: skip
    return rc, out.getvalue(), state["phone"].offer


def _raiser(exc):
    def step9(offer_id):
        raise exc

    return step9


def _errors():
    from orch.custody.base import CustodyError
    from orch.host import frames
    from orch.host.signer import SignerRefused
    from orch.ops.errors import OrchError

    class Wrong(CustodyError):
        code = "custody.wrong_passphrase"

    return [
        SignerRefused("refusing to sign: not confirmed\x1b[2J‮"),
        Wrong("wrong\x1b[2J passphrase"),
        OrchError("host.unavailable", "lost\x1b[2J the connection"),
        frames.FrameError("frame.invalid", "frame\x1b[2J broke"),
        TimeoutError("timed\x1b[2J out"),
        ConnectionError("reset\x1b[2J"),
        RuntimeError("anything\x1b[2J else"),
    ]


@pytest.mark.parametrize("exc", _errors(), ids=lambda e: type(e).__name__)
def test_a_step_9_error_is_a_clean_refusal_never_a_traceback(host, exc):
    rc, text, offer = _drive_to_step9(host, _raiser(exc))
    assert rc != 0
    assert "Traceback" not in text and not any(c in text for c in ("\x1b", "‮"))
    assert f"--resume --offer {offer}" in text  # the offer stays: sign later


def test_declining_the_review_says_nothing_was_signed(host):
    from orch.host.signer import SignerRefused

    rc, text, offer = _drive_to_step9(host, _raiser(SignerRefused("refusing to sign: not confirmed\x1b[2J")))
    assert rc != 0 and "nothing was signed" in text


@pytest.mark.parametrize("which", ["stdin", "stdout", "stderr", "file"])
def test_json_fd_must_be_a_pipe_or_a_socket_never_0_to_2_or_a_file(host, tmp_path, which):
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    f = None
    if which == "file":
        f = open(tmp_path / "link.txt", "w")  # noqa: SIM115 - closed below
        fd = fcntl.fcntl(f.fileno(), fcntl.F_DUPFD, 10)  # a regular file on a descriptor above 3, whatever pytest holds
    else:
        fd = {"stdin": 0, "stdout": 1, "stderr": 2}[which]
    try:
        rc, text = run(lab, h, ns(workspace=wid, json=True, json_fd=fd), sleep=lambda t: None)
    finally:
        if f is not None:
            f.close()
            os.close(fd)
    assert rc != 0 and "invalid.input" in text and engine.list_offers() == []  # refused before any offer
    if which == "file":
        assert (tmp_path / "link.txt").read_text() == ""


# -- B1b: the QR block and its erasure (11A.15.23, L3/L4) ---------------------------------------------------


class TtyOut(io.StringIO):
    def isatty(self):
        return True


def _offer_then_cancel(host, out, **kw):
    lab, h, wid, service, engine, owner, primary = host
    relaypair.primary_set(service, primary.cert(scopes=("look",)), "Mac")
    polls = {"n": 0}

    def sleep(_t):
        polls["n"] += 1
        if polls["n"] == 2:
            engine.cancel(engine.list_offers()[0].offer_id)

    return paircli.main(ns(workspace=wid, no_qr=False, **kw), {}, out, state=lab.state, control=h.host.control_path,
                        sleep=sleep, primary=lambda *a: None)  # fmt: skip


def test_the_terminal_gets_a_qr_of_the_native_link_and_the_link_lines_leave_the_screen(host, monkeypatch):
    import segno

    monkeypatch.setattr(paircli, "_width", lambda: 100)
    out = TtyOut()
    assert _offer_then_cancel(host, out) == 3
    text = out.getvalue()
    link = next(ln for ln in text.splitlines() if ln.startswith("link: "))[len("link: ") :]
    assert link.startswith("orch://pair?relay=")
    block, _, rest = text.partition("state: open\n")
    buf = io.StringIO()
    segno.make(link, error="m").terminal(out=buf, compact=True)
    assert buf.getvalue() in block and "valid 10 min, single use; anyone who sees this can pair" in block
    assert paircli.FALLBACK in block
    rows = paircli._rows(block, 100)
    assert f"\x1b[{rows + 1}A\x1b[J" in rest and "\x1b[3J" not in text and "\x1b[2J" not in text
    assert "removed from the screen" in rest and "state: cancelled" in rest


def test_no_erase_when_stdout_is_not_a_terminal_and_none_with_no_qr_flag_off(host):
    out = io.StringIO()
    assert _offer_then_cancel(host, out) == 3
    assert "\x1b[" not in out.getvalue() and "link: orch://" in out.getvalue()


def test_the_qr_library_missing_still_shows_the_link(host, monkeypatch):
    monkeypatch.setattr(paircli, "_qr", lambda link: None)
    out = io.StringIO()
    _offer_then_cancel(host, out)
    assert "QR library is missing" in out.getvalue() and "link: orch://" in out.getvalue()


def test_erase_rows_count_wrapped_lines():
    assert paircli._rows("a\n" + "b" * 250 + "\n", 80) == 1 + 4
    assert paircli._rows("", 80) == 0


def test_attach_erases_exactly_when_the_offer_leaves_open():
    s = Script([("open", False), ("open", False), ("pending", False), ("rejected", False)])
    calls, out = [], io.StringIO()
    paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "", step9=lambda o: None, sleep=lambda t: None,
                   erase=lambda line=True: calls.append((out.getvalue().count("state:"), line)))  # fmt: skip
    assert calls == [(1, True)]  # once, after "state: open" and before "state: pending"


def test_attach_erases_when_the_first_look_is_already_past_open():
    """A fast phone: the offer is pending at the first look, no "state: open" line was printed, the block is armed."""
    s = Script([("pending", False), ("rejected", False)])
    calls, out = [], io.StringIO()
    paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "", step9=lambda o: None, sleep=lambda t: None,
                   erase=lambda line=True: calls.append((out.getvalue().count("state:"), line)))  # fmt: skip
    assert calls == [(0, False)]  # before the first state line, and without the extra "state: open" row
    calls.clear()
    s = Script([("open", False), ("open", False)])
    paircli.attach(s, "w" * 32, "o" * 32, io.StringIO(), ask=lambda p: "", step9=lambda o: None,
                   sleep=lambda t: None, max_polls=2, erase=lambda line=True: calls.append(line))  # fmt: skip
    assert calls == []  # still open: nothing erased


def test_a_failed_primary_step_says_when_a_new_offer_is_possible():
    import time

    soon = int(time.time() * 1000) + 5 * 60_000
    assert "about 6 minute(s)" in paircli._when_new_offer(soon - 1000) and "relay.pair_open" in paircli._when_new_offer(
        soon
    )
    assert paircli._when_new_offer(None).startswith("start a new one")
    assert paircli._when_new_offer(1).startswith("start a new one")
    s = InlineScript([("pending", False), ("cert_pending", False)])
    out = io.StringIO()

    class NoKey(Exception):
        until_ms = soon

    def primary(*a):
        raise NoKey

    assert paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                          sleep=lambda t: None, primary=primary) == 3  # fmt: skip
    assert "a new offer is possible in about" in out.getvalue()


def test_a_rejection_that_r6_already_saw_is_not_a_failure():
    """The rejection posted to the relay can reach R6's own poll before ``relay.pair_cert`` does (T2 saw it once)."""
    s = InlineScript([("pending", False), ("cert_pending", False), ("rejected", False)])
    base = s.__call__

    def refuse_once(op, args):
        if op == "host.relay_pair_cert":
            s.states = [("rejected", False)]
            raise paircli._Refused({"code": "relay.pair_closed"}, 3)
        return base(op, args)

    out = io.StringIO()
    rc = paircli.attach(refuse_once, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=lambda *a: Outcome(rejection={"o": {}, "sig": "r"}))  # fmt: skip
    assert rc == 3 and "the pairing ended: rejected" in out.getvalue() and "primary step ended" not in out.getvalue()
    s2 = InlineScript([("pending", False), ("cert_pending", False)])
    base2 = s2.__call__

    def refuse_always(op, args):
        if op == "host.relay_pair_cert":
            raise paircli._Refused({"code": "relay.pair_closed"}, 3)
        return base2(op, args)

    out = io.StringIO()
    rc = paircli.attach(refuse_always, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=lambda *a: Outcome(rejection={"o": {}, "sig": "r"}))  # fmt: skip
    assert rc == 3 and "the host refused: relay.pair_closed" in out.getvalue() and s2.cancelled


def test_a_certificate_issued_but_the_hand_over_failing_is_told_to_the_owner():
    s = InlineScript([("pending", False), ("cert_pending", False)])
    base = s.__call__

    def refuse(op, args):
        if op == "host.relay_pair_cert":
            raise paircli._Refused({"code": "relay.quota"}, 3)
        return base(op, args)

    out = io.StringIO()
    rc = paircli.attach(refuse, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=lambda *a: Outcome(cert={"o": {}}, device_id="d_" + "ab" * 16),
                        )  # fmt: skip
    text = out.getvalue()
    assert rc == 3 and "a certificate was issued for device d_abababab but the pairing did not complete" in text
    assert "no command to revoke it yet" in text


def test_a_certificate_issued_and_the_offer_then_ending_is_told_once():
    s = InlineScript([("pending", False), ("cert_pending", False), ("cert_pending", False), ("failed", False)])
    out = io.StringIO()
    rc = paircli.attach(s, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None,
                        sleep=lambda t: None, primary=lambda *a: Outcome(cert={"o": {}}, device_id="d_" + "cd" * 16),
                        )  # fmt: skip
    text = out.getvalue()
    assert rc == 3 and "the pairing ended: failed" in text and text.count("a certificate was issued for device") == 1
    assert "d_cdcdcdcd" in text
    ok = InlineScript([("pending", False), ("cert_pending", False), ("cert_pending", True), ("approved", True)])
    out = io.StringIO()
    paircli.attach(ok, "w" * 32, "o" * 32, out, ask=lambda p: "ABCDEF", step9=lambda o: None, sleep=lambda t: None,
                   primary=lambda *a: Outcome(cert={"o": {}}, device_id="d_" + "cd" * 16))  # fmt: skip
    assert "certificate was issued" not in out.getvalue()


def test_install_origin_checks_the_given_relay_like_init_does(tmp_path):
    from orch.custody import primary_register

    st = tmp_path / "state"
    (st / "person" / "primary").mkdir(parents=True, mode=0o700)
    primary_register._write_private(
        primary_register.relay_path(st), {"v": 1, "origin": ORIGIN, "person_id": "0" * 32, "device_id": "1" * 32,
                                          "label": "x"})  # fmt: skip
    assert paircli._install_origin(st, ORIGIN) == ORIGIN
    with pytest.raises(Exception, match="--relay"):
        paircli._install_origin(st, "not an origin")
    with pytest.raises(Exception, match="--relay"):  # a path is not an origin: the same check as `primary init`
        paircli._install_origin(st, ORIGIN + "/x")


def test_link_and_primary_set_name_the_next_command(host, tmp_path, monkeypatch):
    lab, h, wid, service, engine, owner, primary = host
    _registered_primary(lab, lab.ws.owner, tmp_path, monkeypatch)
    err = io.StringIO()
    rc, text = run(lab, h, ns(action="primary", sub="set", workspace=wid), err=err)
    assert rc == 0 and f"next: orch host relay pair --workspace {wid}\n" in err.getvalue()
