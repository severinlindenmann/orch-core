"""H5c CLI: ``orch host convenience on|off|show`` (the per-workspace flag) and ``orch keys`` (custody transitions)."""

from __future__ import annotations

import io
import json
import os

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


def test_the_flag_is_switched_per_workspace_by_the_host_cli(served, monkeypatch):
    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)
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


def test_convenience_defaults_to_the_workspace_of_the_current_folder(served, monkeypatch):
    """F3: inside a workspace folder `--workspace` is that folder's workspace; elsewhere the error names the fix."""
    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)
    state = str(served.lab.state)
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    monkeypatch.chdir(served.service.entry.root)
    monkeypatch.setenv("PWD", os.getcwd())
    code, out, _ = run("host", "convenience", "on", "--state", state)  # `on` never defaults to the folder
    assert code == 5 and "--workspace is required" in out
    assert f"convenience on --workspace {served.service.entry.prefix}" in out  # the folder's prefix, not a placeholder
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert code == 0 and not convenience.enabled(served.lab.state, served.wid, UID)
    assert "this folder belongs to" in out and served.service.entry.prefix in out
    assert "one-click custody" not in out and "orch sign" not in out  # no key held here: neither claim is made
    from orch.custody import select

    real = select.status
    monkeypatch.setattr(select, "status", lambda st, wid: {**real(st, wid), "custody": "passphrase"})
    code, out, _ = run("host", "convenience", "off", "--state", state)
    # a key already in passphrase custody signs in the Terminal: no "stays in one-click custody" claim
    assert "Sign in the Terminal with orch sign" in out and "stays in one-click custody" not in out
    monkeypatch.setattr(select, "status", lambda st, wid: {**real(st, wid), "custody": "convenience"})
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert (
        code == 0 and "stays in one-click custody" in out and "orch keys custody passphrase" in out
    )  # F1: the way to a passphrase
    assert "Sign in the Terminal with orch sign" not in out
    monkeypatch.setattr(select, "status", real)
    monkeypatch.chdir("/")
    monkeypatch.setenv("PWD", "/")
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert code == 5 and "invalid.input" in out
    assert "describe describe" not in out and "convenience off --workspace <your workspace prefix>" in out
    code, out, _ = run("host", "convenience", "on", "--state", state)
    assert code == 5 and "convenience on --workspace <your workspace prefix>" in out


def test_orch_keys_converts_only_with_the_flag_and_moves_back_only_in_place(served, monkeypatch):
    state = served.lab.state
    provider = Provider()
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    pub = human.open_backend(human.keys_dir(state, served.wid)).create(
        "dk", role="device", passphrase=provider.passphrase
    )
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    from orch.custody import cli as keys_cli

    monkeypatch.setattr(keys_cli, "has_terminal", lambda: False)
    base0 = ("--workspace", served.wid, "--state", str(state), "--json")
    code, out, _ = run("keys", "custody", "convenience", *base0)
    err0 = json.loads(out)["error"]
    assert code == 3 and err0["code"] == "custody.no_prompt" and "retryable" in err0 and "hint" in err0
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: True)
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


def test_the_on_hint_names_the_prefix_only_when_the_folder_is_not_reached_through_a_symlink(
    served, monkeypatch, tmp_path
):
    """A copyable hint must not be steerable: a planted symlink ($PWD != real cwd) gets the placeholder."""
    state = str(served.lab.state)
    link = tmp_path / "link"
    link.symlink_to(served.service.entry.root)
    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))  # the logical path differs from the physical one
    code, out, _ = run("host", "convenience", "on", "--state", state)
    assert code == 5 and "convenience on --workspace <your workspace prefix>" in out
    assert served.service.entry.prefix not in out
    monkeypatch.chdir(served.service.entry.root)
    monkeypatch.setenv("PWD", os.getcwd())
    code, out, _ = run("host", "convenience", "on", "--state", state)
    assert f"convenience on --workspace {served.service.entry.prefix}" in out
