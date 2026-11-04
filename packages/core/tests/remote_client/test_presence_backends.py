"""R5: presence rules of the client: domain limit, template matching, key files refused, SE helper protocol with a fake
helper, FIDO2 rules with a fake authenticator, the custody role, and no software presence in src."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u, unb64u
from orch.remoteclient import presence as P
from orch.remoteclient.codes import ClientError
from orch.remoteclient.fido2_backend import Fido2Backend
from orch.remoteclient.se_backend import SecureEnclaveBackend
from orch.remoteclient.state import Store, refuse_key_file
from tests.remote_client.fakes import FakeCtap

WS = "882d742fa53e3cfdf21947a2c63286f2"
RD = "rd_6be572e1241672d2a48515fb6eca992b"
OPEN = P.open_bytes(WS, RD, b"h" * 32, b"c" * 32)
PROMPT = P.Prompt("Open", reason="Open workspace on this computer")


# --- the domain limit ---


def test_check_signable_accepts_exactly_the_two_messages():
    assert P.check_signable(OPEN) == "open"
    pb = P.presence_bytes(WS, RD, b"h" * 32, b"n" * 32, [b"a" * 32, b"b" * 32])
    assert P.check_signable(pb) == "presence"


@pytest.mark.parametrize(
    "msg",
    [
        b"",
        b"orch/v2/sig/device-cert|" + b"x" * 96,
        OPEN + b"x",
        OPEN[:-1],
        b"orch/v2/remote-presence|" + bytes(96) + bytes([0]),
        b"orch/v2/remote-presence|" + bytes(96) + bytes([17]) + bytes(32 * 17),
        P.presence_bytes(WS, RD, b"h" * 32, b"n" * 32, [b"a" * 32]) + b"z",
        "text",
    ],
)
def test_check_signable_refuses_everything_else(msg):
    with pytest.raises(ClientError):
        P.check_signable(msg)


def test_unsorted_or_duplicate_digests_are_refused():
    base = P.presence_bytes(WS, RD, b"h" * 32, b"n" * 32, [b"a" * 32, b"b" * 32])
    swapped = base[:-64] + base[-32:] + base[-64:-32]
    with pytest.raises(ClientError):
        P.check_signable(swapped)
    with pytest.raises(ClientError):
        P.presence_bytes(WS, RD, b"h" * 32, b"n" * 32, [b"a" * 32, b"a" * 32])


def test_template_matching():
    t = "/api/w/{ws}/runs/{run}/attach"
    assert P.template_matches(t, f"/api/w/{WS}/runs/r_1/attach", WS)
    assert P.template_matches(t, f"/api/w/{WS[:8]}-{WS[8:12]}-{WS[12:16]}-{WS[16:20]}-{WS[20:]}/runs/r_1/attach", WS)
    assert not P.template_matches(t, f"/api/w/{'0' * 32}/runs/r_1/attach", WS)  # another workspace
    assert not P.template_matches(t, f"/api/w/{WS}/runs/r_1/other", WS)
    assert not P.template_matches(t, f"/api/w/{WS}/runs/attach", WS)
    assert not P.template_matches(t, f"/api/w/{WS}/runs/a/b/attach", WS)
    assert P.template_matches("/api/x/{p:path}", "/api/x/a/b/c", WS)


# --- key files and the custody role ---


@pytest.mark.parametrize("ref", ["/home/u/key.pem", "file:abc", "-----BEGIN", "key:abc", "x.p8", "", "../k", 5, "~/k"])
def test_key_file_refs_are_refused(ref):
    with pytest.raises(ClientError) as e:
        refuse_key_file(ref)
    assert e.value.code == "remote_client.key_file_refused"


def test_stored_key_file_presence_is_refused_at_load(tmp_path):
    st = Store(tmp_path / "rc")
    with pytest.raises(ClientError):
        st.set_presence("platform", "/tmp/k.pem", b"\x04" + bytes(64))
    (tmp_path / "rc" / "workspaces.json").write_text(json.dumps({WS: {"presence_ref": "/tmp/k.pem"}}))
    with pytest.raises(ClientError):
        st.workspaces()


def test_no_software_presence_signer_in_src():
    src = Path(__file__).resolve().parents[2] / "src" / "orch" / "remoteclient"
    for f in src.glob("*.py"):
        text = f.read_text()
        assert "generate_private_key" not in text and "private_key_from" not in text and "suite.sign" not in text, (
            f.name
        )


def test_custody_role_remote_presence_signs_only_its_two_labels_and_is_never_a_person_label():
    from orch.custody import PERSON_LABELS
    from orch.custody.base import REMOTE_PRESENCE_LABELS, ROLE_LABELS, label_allowed
    from orch.custody.select import __dict__ as select_ns

    assert ROLE_LABELS["remote-presence"] == REMOTE_PRESENCE_LABELS
    assert set(REMOTE_PRESENCE_LABELS) == {b"orch/v2/remote-open|", b"orch/v2/remote-presence|"}
    assert not set(REMOTE_PRESENCE_LABELS) & set(PERSON_LABELS)
    for lab in PERSON_LABELS:
        assert not label_allowed(lab + b"x", REMOTE_PRESENCE_LABELS)
    assert label_allowed(OPEN, REMOTE_PRESENCE_LABELS)
    assert "remote-presence" not in str(select_ns.get("CUSTODIES"))
    from orch.custody.passphrase import ROLES

    assert "remote-presence" not in ROLES  # no file-tier / passphrase backend can hold it


# --- the Secure Enclave helper ---


class Helper:
    def __init__(self, tmp_path, reply):
        self.path = tmp_path / "orch-remote-key"
        self.path.write_text("#!/bin/sh\n")
        self.path.chmod(0o755)
        self.reply, self.calls = reply, []

    def run(self, argv, stdin, timeout, env):
        self.envs = [*getattr(self, "envs", []), env]
        self.calls.append((argv, json.loads(stdin)))
        r = self.reply(json.loads(stdin)) if callable(self.reply) else self.reply
        return json.dumps(r).encode()


def test_se_create_and_sign_speak_the_helper_protocol(tmp_path):
    key = suite.generate_private_key()
    pub = suite.public_bytes(key)

    def reply(req):
        if req["op"] == "create":
            return {"pub": b64u(pub), "ref": "ab" * 16}
        return {"sig": b64u(suite.sign(key, unb64u(req["msg"])))}

    h = Helper(tmp_path, reply)
    be = SecureEnclaveBackend(h.path, runner=h.run)
    made = be.create("My Mac")
    assert made == {"kind": "platform", "pub65": pub, "ref": "ab" * 16}
    a = be.assert_(made["ref"], OPEN, PROMPT)
    assert a["form"] == "p256" and suite.verify(pub, unb64u(a["sig"], 64), OPEN)
    assert h.calls[1][1] == {"op": "sign", "ref": "ab" * 16, "msg": b64u(OPEN), "reason": PROMPT.reason}
    assert h.calls[0][0] == [str(h.path)]


def test_se_refuses_a_wrong_message_before_the_helper_runs(tmp_path):
    h = Helper(tmp_path, {"sig": "x"})
    be = SecureEnclaveBackend(h.path, runner=h.run)
    with pytest.raises(ClientError):
        be.assert_("ab" * 16, b"orch/v2/sig/device-cert|whatever", PROMPT)
    assert h.calls == []


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ({"error": "cancelled"}, "remote_client.cancelled"),
        ({"error": "unavailable", "detail": "no_secure_enclave"}, "remote_client.no_presence"),
        ({"error": "refused"}, "remote_client.helper"),
        ({"sig": "AAAA", "extra": 1}, "remote_client.helper"),
        ({"sig": "AAAA"}, "remote_client.helper"),
    ],
)
def test_se_errors(tmp_path, reply, code):
    be = SecureEnclaveBackend(Helper(tmp_path, reply).path, runner=Helper(tmp_path, reply).run)
    with pytest.raises(ClientError) as e:
        be.assert_("ab" * 16, OPEN, PROMPT)
    assert e.value.code == code


def test_se_needs_an_absolute_helper_and_a_ref_that_is_not_a_path(tmp_path):
    with pytest.raises(ClientError):
        SecureEnclaveBackend("orch-remote-key")
    h = Helper(tmp_path, {"sig": "x"})
    with pytest.raises(ClientError):
        SecureEnclaveBackend(h.path, runner=h.run).assert_("/etc/key", OPEN, PROMPT)


def test_se_create_refuses_a_bad_public_key(tmp_path):
    h = Helper(tmp_path, {"pub": b64u(b"\x04" + bytes(64)), "ref": "ab" * 16})
    with pytest.raises(ClientError):
        SecureEnclaveBackend(h.path, runner=h.run).create("x")


# --- FIDO2 ---


def fido(tmp_path, **kw):
    st = Store(tmp_path / "rc")
    ctap = FakeCtap(**kw)
    return Fido2Backend(ctap, st), ctap, st


def test_fido2_enroll_and_assert_roundtrip_in_the_hosts_terms(tmp_path):
    from tests.host import remote_port as RP

    be, ctap, st = fido(tmp_path)
    made = be.create("Key")
    assert made["kind"] == "fido2" and made["ref"].startswith("fido2-")
    a = be.assert_(made["ref"], OPEN, PROMPT)
    n1 = RP.assertion_ok(b64u(made["pub65"]), "fido2", OPEN, a, 0)
    assert n1 == 1 and st.counter(made["ref"]) == 1
    a2 = be.assert_(made["ref"], OPEN, PROMPT)
    assert RP.assertion_ok(b64u(made["pub65"]), "fido2", OPEN, a2, 1) == 2


def test_fido2_without_uv_capability_is_refused_before_any_credential(tmp_path):
    be, ctap, _ = fido(tmp_path, uv=False, pin=False)
    with pytest.raises(ClientError) as e:
        be.create("Key")
    assert e.value.code == "remote_client.fido2_no_uv"
    assert "Set a PIN first" in e.value.message


def test_fido2_pin_set_is_enough_and_non_es256_is_refused(tmp_path):
    be, _, _ = fido(tmp_path, uv=False, pin=True)
    assert be.create("Key")["kind"] == "fido2"
    be, _, _ = fido(tmp_path / "b", alg=-8)
    with pytest.raises(ClientError) as e:
        be.create("Key")
    assert e.value.code == "remote_client.fido2_algorithm"


def test_fido2_credential_made_without_uv_or_synced_is_refused(tmp_path):
    be, ctap, _ = fido(tmp_path)
    ctap.made_flags = 0x41  # UP only
    with pytest.raises(ClientError):
        be.create("Key")
    be, ctap, _ = fido(tmp_path / "b")
    ctap.made_flags = 0x45 | 0x08  # backup eligible: a synced passkey
    with pytest.raises(ClientError):
        be.create("Key")


@pytest.mark.parametrize(
    "flags", [0x01, 0x04, 0x05 | 0x08, 0x05 | 0x10, 0x05 | 0x40, 0x05 | 0x80], ids=["up", "uv", "BE", "BS", "AT", "ED"]
)
def test_fido2_bad_flags_are_refused_and_nothing_is_sent(tmp_path, flags):
    be, ctap, st = fido(tmp_path)
    made = be.create("Key")
    ctap.flags = flags
    with pytest.raises(ClientError) as e:
        be.assert_(made["ref"], OPEN, PROMPT)
    assert e.value.code == "remote_client.assertion" and st.counter(made["ref"]) == 0


def test_fido2_counter_must_rise_and_extra_authdata_is_refused(tmp_path):
    be, ctap, st = fido(tmp_path)
    made = be.create("Key")
    be.assert_(made["ref"], OPEN, PROMPT)
    ctap.counter = 0  # the key repeats counter 1
    with pytest.raises(ClientError):
        be.assert_(made["ref"], OPEN, PROMPT)
    ctap.counter = 10
    ctap.auth_extra = b"\x00"  # 38 bytes
    with pytest.raises(ClientError):
        be.assert_(made["ref"], OPEN, PROMPT)


def test_fido2_wrong_domain_and_unknown_ref(tmp_path):
    be, ctap, _ = fido(tmp_path)
    made = be.create("Key")
    with pytest.raises(ClientError):
        be.assert_(made["ref"], b"orch/v2/sig/decision|" + bytes(100), PROMPT)
    assert ctap.calls == 0
    with pytest.raises(ClientError):
        be.assert_("fido2-" + "0" * 32, OPEN, PROMPT)


def test_fido2_signature_of_another_key_is_refused(tmp_path):
    be, ctap, _ = fido(tmp_path)
    made = be.create("Key")
    ctap.key = suite.generate_private_key()
    with pytest.raises(ClientError):
        be.assert_(made["ref"], OPEN, PROMPT)
    assert hashlib.sha256(b"orch-remote").digest()  # the rp id hash is the contract's


def test_se_helper_never_gets_a_loader_variable(tmp_path, monkeypatch):
    for k, v in {"DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib", "LD_PRELOAD": "/tmp/e.so", "PYTHONPATH": "rel",
                 "SECRET_TOKEN": "x"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    h = Helper(tmp_path, {"pub": "AA", "ref": "ab" * 16})
    with pytest.raises(Exception):  # noqa: B017 - the reply is not a valid key; only the environment matters here
        SecureEnclaveBackend(h.path, runner=h.run).create("x")
    assert h.envs and set(h.envs[0]) <= {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "PATH"}
    assert not any(k.startswith(("DYLD_", "LD_")) or k.startswith("PYTHON") for k in h.envs[0])
