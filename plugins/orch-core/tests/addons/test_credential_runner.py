"""V3 G6 (#924) C2/C3 end to end: the stored key (``orch.addons.credential``) through ``runner.call``.

T0: ``uv run pytest tests/addons/test_credential_runner.py -x -q``"""

from __future__ import annotations

import hashlib
import logging
import os

import pytest

from orch.addons import credential as cr
from orch.addons.runner import Limits, call
from tests.addons.test_runner_1_1 import make, refused

UID = os.getuid()
SECRET = "sk-live-SECRET-0123456789_abcdef"
DIGEST = hashlib.sha256(SECRET.encode()).hexdigest()


@pytest.fixture
def state(tmp_path):
    root = tmp_path / "state"
    (root / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    cr.store(root, "ws", UID, "m11", cr.Secret(SECRET), 1.0)
    return root


def provider(state, granted, name="m11"):
    return cr.provider(state, "ws", UID, name, granted.capabilities, active=granted.enabled and not granted.purged)


def test_the_add_on_gets_its_key_only_with_credential_and_network(tmp_path, state):
    package, granted = make(tmp_path / "a", caps=("credential", "network"))
    out = call(package, granted, "propose", {"mode": "params"}, credential=provider(state, granted))
    assert out["params"]["credential"] == DIGEST  # the child saw it, on stdin
    for caps in (("network",), ("choose_model",)):  # the manifest schema refuses credential without network
        package, granted = make(tmp_path / ("b" + "".join(caps)), caps=caps)
        out = call(
            package,
            granted,
            "propose" if "choose_model" not in caps else "launch.model",
            {"mode": "params"},
            credential=provider(state, granted),
        )
        assert "credential" not in out["params"]


def test_another_addons_call_never_gets_the_key(tmp_path, state):
    package, granted = make(tmp_path, caps=("credential", "network"))
    out = call(package, granted, "propose", {"mode": "params"}, credential=provider(state, granted, name="other"))
    assert "credential" not in out["params"]


def test_the_key_is_never_in_env_argv_logs_or_state_files(tmp_path, state, caplog):
    caplog.set_level(logging.DEBUG)
    package, granted = make(tmp_path, caps=("credential", "network"))
    out = call(package, granted, "propose", {"mode": "env_args"}, credential=provider(state, granted))
    assert SECRET not in repr(out)  # env and argv of the child
    assert SECRET not in caplog.text
    for p in state.rglob("*"):
        if p.is_file() and p != state / "hosts" / "ws" / "addons" / "m11" / "credential":
            assert SECRET.encode() not in p.read_bytes()


def test_stderr_of_a_failing_call_is_redacted(tmp_path, state):
    package, granted = make(tmp_path, caps=("credential", "network"))
    e = refused(
        "addon.crashed", call, package, granted, "propose", {"mode": "crash_credential"},
        credential=provider(state, granted),
    )  # fmt: skip
    assert SECRET not in e.stderr and SECRET not in str(e) and SECRET not in repr(e.__dict__)
    for cap in range(0, 70, 3):  # a cut at every few offsets leaves no start of it either
        e = refused(
            "addon.crashed", call, package, granted, "propose", {"mode": "crash_credential"}, Limits(max_stderr=cap),
            credential=provider(state, granted),
        )  # fmt: skip
        assert SECRET[:4] not in e.stderr


@pytest.mark.parametrize("mode", ["echo_credential", "echo_credential_escaped", "echo_credential_key"])
def test_an_answer_that_echoes_the_key_is_refused(tmp_path, state, mode):
    package, granted = make(tmp_path, caps=("credential", "network"))
    e = refused("addon.bad_response", call, package, granted, "propose", {"mode": mode},
                credential=provider(state, granted))  # fmt: skip
    assert SECRET not in str(e) and SECRET not in e.stderr


def test_a_disabled_or_dropped_grant_gets_no_key_and_a_compromised_file_is_not_read_for_it(tmp_path, state):
    package, granted = make(tmp_path, caps=("choose_model", "network"))  # no credential capability
    (state / "hosts" / "ws" / "addons" / "m11" / "credential").chmod(0o644)  # would be refused if it were opened
    out = call(package, granted, "launch.model", {"mode": "params"}, credential=provider(state, granted))
    assert "credential" not in out["params"]
