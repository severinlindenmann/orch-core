"""T2 for the dev-pair clients against the REAL orch-relay of ``SOURCE`` (loopback only): the sandbox owner creates its
account with the operator's invite (``POST /v2/accounts`` with the sandbox ``PK`` and the primary's certificate), the
relay serves the owner's device login, the same account is refused a second time, and the reference phone's HTTP net
logs a device in with the protocol's login layout. The pairing and column-R legs are the pairing T2 test's."""

from __future__ import annotations

import json
import os

import pytest

from orch import crypto, devsandbox
from orch.identity import recovery as rec
from orch.relay_owner import OwnerRefused

from ..owner import SandboxOwner
from ..phone import HttpNet, PhoneError
from .harness import RelayProcess

pytestmark = pytest.mark.relay_t2

FAST = 2**10
WS = "ab" * 16


def sandbox(tmp_path):
    root = tmp_path / "sbx"
    os.mkdir(root, 0o700)
    code = rec.generate_recovery_code()
    pid = crypto.person_id(crypto.public_bytes(rec._key_at(code, FAST))).hex()
    devsandbox.write_manifest(root, "cd" * 8, person_id="p_" + pid)
    os.mkdir(root / "secrets", 0o700)
    fd = os.open(root / "secrets" / "recovery-code", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, code.encode())
    os.close(fd)
    return root, pid


def test_the_sandbox_owner_creates_an_account_and_logs_in(relay: RelayProcess, tmp_path):
    root, pid = sandbox(tmp_path)
    owner = SandboxOwner(root, relay.origin, kdf_n=FAST)
    try:
        invite = relay.admin("invite", "create", "--ttl", "1h").strip()
        cert = owner.create_account(invite)
        assert cert.exists() and (os.stat(cert).st_mode & 0o777) == 0o600
        owner.client.login()  # the owner's device login against the real relay
        assert owner.client.logged_in
        with owner.person_key({"sig_device_cert"}) as pk, pytest.raises(OwnerRefused):
            owner.client.create_account(invite, pk.pk_pub, json.loads(cert.read_bytes()))  # single-use invite
        # the phone's net: the same login layout, signed by the device key
        net = HttpNet(relay.origin, WS)
        net.login(owner.keys.device_id.hex(), owner.keys.sign)
        assert net.token
        with pytest.raises(PhoneError):
            net.members()  # not a member of WS: the relay refuses, the phone reports it
    finally:
        owner.close()
