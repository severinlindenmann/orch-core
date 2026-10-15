"""The dev sandbox's person-key source (11A.15.11; test tree only): refuses without the sandbox manifest naming its
root, implements the production ``PersonKeySource`` contract and label policy, and signs a cert challenge the host
accepts. Generated codes only."""

from __future__ import annotations

import json
import os

import pytest

from orch import crypto
from orch.crypto.labels import L
from orch.custody import personkey
from orch.identity import certreq
from orch.identity import recovery as rec

from ..custody.test_personkey import _request_for
from .personkey_sandbox import CODE_FILE, MANIFEST, SECRETS, SandboxPersonKey, SandboxRefused, make_sandbox

FAST = 2**10
ALL = personkey.SIGN_LABELS


@pytest.fixture
def sandbox(tmp_path):
    code = rec.generate_recovery_code()
    root = tmp_path / "h12cd-sb"
    make_sandbox(root, code)
    key = rec._key_at(code, FAST)
    return root, key, crypto.person_id(crypto.public_bytes(key))


def test_the_sandbox_source_is_a_person_key_source_and_signs_a_challenge_the_host_accepts(sandbox):
    root, key, pid = sandbox
    pk_pub = crypto.public_bytes(key)
    signed, req, inner = _request_for(pk_pub, lambda p: crypto.sign(key, p))
    src = SandboxPersonKey(root, labels=("sig_cert_challenge",), person_id=pid, _kdf_n=FAST)
    with src as pk:
        assert isinstance(pk, personkey.PersonKeySource)
        with pytest.raises(personkey.LabelRefused):
            pk.sign("sig_revocation", L["sig_revocation"] + b"{}")
        ch = certreq.make_cert_challenge(pk.pk_pub, pk.signer("sig_cert_challenge"), req, inner, os.urandom(32))
        with pytest.raises(personkey.LabelRefused):
            pk.sign("sig_ws_delegation", L["sig_ws_delegation"] + b"{}")
        with pytest.raises(personkey.LabelRefused):
            pk.signer("sig_decision")
    assert certreq.check_cert_challenge(ch, pk_pub, signed["o"]).device_id == inner.device_id
    with pytest.raises(personkey.PersonKeyClosed):
        src.sign("sig_revocation", L["sig_revocation"] + b"{}")


def test_without_the_manifest_the_sandbox_source_refuses(sandbox):
    root, _, pid = sandbox
    os.unlink(root / MANIFEST)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_a_manifest_of_another_root_is_refused(sandbox, tmp_path):
    root, _, pid = sandbox
    other = tmp_path / "h12cd-other"
    os.mkdir(other, 0o700)
    os.mkdir(other / SECRETS, 0o700)
    for rel in (MANIFEST, f"{SECRETS}/{CODE_FILE}"):
        fd = os.open(other / rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (root / rel).read_bytes())
        os.close(fd)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(other, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_a_symlinked_manifest_or_root_is_refused(sandbox, tmp_path):
    root, _, pid = sandbox
    link = tmp_path / "h12cd-link"
    os.symlink(root, link)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(link, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()
    real = tmp_path / "h12cd-manifest"
    os.rename(root / MANIFEST, real)
    os.symlink(real, root / MANIFEST)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


@pytest.mark.parametrize("target", [MANIFEST, f"{SECRETS}/{CODE_FILE}", "."])
def test_loose_permissions_are_refused(sandbox, target):
    root, _, pid = sandbox
    os.chmod(root / target, 0o755 if target == "." else 0o644)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_a_manifest_without_an_id_is_refused(sandbox):
    root, _, pid = sandbox
    doc = json.loads((root / MANIFEST).read_text())
    del doc["id"]
    (root / MANIFEST).write_text(json.dumps(doc))
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_another_person_is_refused_and_nothing_is_held(sandbox):
    root, _, _ = sandbox
    src = SandboxPersonKey(root, labels=ALL, person_id=bytes(16), _kdf_n=FAST)
    with pytest.raises(personkey.PersonMismatch):
        src.__enter__()
    with pytest.raises(personkey.PersonKeyClosed):
        src.derive_label_key()


def test_the_sandbox_label_key_is_the_production_derivation(sandbox):
    root, key, pid = sandbox
    from orch.ops import workspace_init

    with SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST) as pk:
        assert pk.derive_label_key() == workspace_init._label_key(crypto.private_scalar(key))


def test_loose_secrets_directory_is_refused(sandbox):
    """Opus F7: ``secrets/`` is this user's, mode 0700."""
    root, _, pid = sandbox
    os.chmod(root / SECRETS, 0o750)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_a_fifo_in_place_of_the_code_file_is_refused_without_blocking(sandbox):
    """Opus F7: the code file is opened with O_NONBLOCK | O_NOFOLLOW and must be a regular file (a FIFO would block a
    plain open forever)."""
    root, _, pid = sandbox
    os.unlink(root / SECRETS / CODE_FILE)
    os.mkfifo(root / SECRETS / CODE_FILE, 0o600)
    with pytest.raises(SandboxRefused):
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()


def test_a_bad_sandbox_code_leaves_no_text_in_the_exception(sandbox):
    """Opus F1, applied to the sandbox: it shares the production parse-and-derive step."""
    from ..custody.test_personkey import _leaks, _secret_code_with_bad_checksum

    root, _, pid = sandbox
    typed = _secret_code_with_bad_checksum()
    path = root / SECRETS / CODE_FILE
    os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, typed.encode())
    os.close(fd)
    with pytest.raises(personkey.BadRecoveryCode) as e:
        SandboxPersonKey(root, labels=ALL, person_id=pid, _kdf_n=FAST).__enter__()
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, typed, None) == []
