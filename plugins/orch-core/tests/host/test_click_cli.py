"""H5c CLI: ``orch host convenience on|off|show`` (the per-workspace flag) and ``orch keys`` (custody transitions)."""

from __future__ import annotations

import io
import json

from orch.cli.main import main
from orch.custody import PassphraseBackend
from orch.custody import convenience as conv
from orch.custody.convenience import FakeStore
from orch.host import convenience
from orch.ops import human
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.ops.humans import KDF, Provider

lab, served, human_fixture = human_helpers.lab, human_helpers.served, human_helpers.human


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=env or {"HOME": "/nonexistent"}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_the_flag_is_switched_per_workspace_by_the_host_cli(served):
    state = str(served.lab.state)
    assert not convenience.enabled(served.lab.state, served.wid, UID)
    code, out, _ = run("host", "convenience", "on", "--workspace", served.service.entry.prefix, "--state", state)
    assert code == 0 and "agents running as you, or anything that takes over this dashboard" in out
    assert convenience.enabled(served.lab.state, served.wid, UID)
    assert (served.lab.state / "host" / "convenience.json").stat().st_mode & 0o777 == 0o600
    code, out, _ = run("host", "convenience", "off", "--workspace", served.wid, "--state", state)
    assert code == 0 and not convenience.enabled(served.lab.state, served.wid, UID)
    code, out, _ = run("host", "convenience", "on", "--workspace", "NOPE", "--state", state)
    assert code == 2
    code, out, _ = run(
        "host", "convenience", "on", "--workspace", served.wid, "--state", state, env={"ORCH_GRANT": "x"}
    )
    assert code == 3 and "human_only" in out


def test_orch_keys_converts_only_with_the_flag_and_moves_back_only_in_place(served, monkeypatch):
    state = served.lab.state
    provider = Provider()
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    pub = human.open_backend(human.keys_dir(state, served.wid)).create(
        "dk", role="device", passphrase=provider.passphrase
    )
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    base = ("--workspace", served.wid, "--state", str(state), "--json")
    code, out, _ = run("keys", "custody", "convenience", *base)
    assert code == 3 and json.loads(out)["error"]["code"] == "sign.convenience_unavailable"
    code, out, _ = run("keys", "custody", "convenience", *base, env={"ORCH_GRANT": "x"})
    assert json.loads(out)["error"]["code"] == "human_only"
    convenience.set_enabled(state, served.wid, True, UID)
    code, out, err = run("keys", "custody", "convenience", *base)
    assert code == 0, out
    assert "could sign" in err
    res = json.loads(out)["result"]
    assert res["custody"] == "convenience"
    code, out, _ = run("keys", "custody", "status", "--state", str(state), "--json")
    (row,) = json.loads(out)["result"]["workspaces"]
    assert row["custody"] == "convenience" and row["enabled"] is True
    code, out, _ = run("keys", "custody", "passphrase", *base)
    assert code == 5 and "--in-place" in json.loads(out)["error"]["message"]
    code, out, _ = run("keys", "custody", "passphrase", "--rotate", *base)
    assert json.loads(out)["error"]["code"] == "not_implemented"
    monkeypatch.setattr(PassphraseBackend, "create", _with_passphrase(PassphraseBackend.create, provider.passphrase))
    code, out, err = run("keys", "custody", "passphrase", "--in-place", *base)
    assert code == 0, out
    assert "--rotate" in err and json.loads(out)["result"]["custody"] == "passphrase"
    assert human.open_backend(human.keys_dir(state, served.wid)).public_key("dk") == pub
    code, out, _ = run("keys", "forget", *base)
    assert code == 0


def _with_passphrase(create, passphrase):
    def wrapped(self, key_id, *, secret=None, role="device", passphrase_=None, **kw):
        kw.pop("passphrase", None)
        return create(self, key_id, secret=secret, role=role, passphrase=passphrase)

    return wrapped
